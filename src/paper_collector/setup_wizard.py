"""Interactive first-run setup shared by install.sh and install.ps1.

Every optional feature (translation, Baidu Netdisk, notification channel, HTML
reading notes, schedule) can be switched off. Secrets are only ever written to
the local, gitignored .env with owner-only permissions.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PDF2ZH_SPEC = "pdf2zh==1.9.11"
# pdf2zh 1.9.11 and its pinned onnxruntime do not support Python 3.13 yet.
PDF2ZH_PYTHON = "3.12"
ENV_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def set_toml_value(text: str, section: str, key: str, value: object) -> str:
    """Set ``key`` inside ``[section]`` while keeping comments and layout intact."""
    literal = json.dumps(value, ensure_ascii=False)
    lines = text.splitlines()
    header = f"[{section}]"
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == header)
    except StopIteration:
        return text.rstrip("\n") + f"\n\n{header}\n{key} = {literal}\n"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start + 1, end):
        if pattern.match(lines[i]):
            lines[i] = f"{key} = {literal}"
            break
    else:
        lines.insert(start + 1, f"{key} = {literal}")
    return "\n".join(lines) + "\n"


def update_env(path: Path, values: dict[str, str], template: Path | None = None) -> None:
    """Merge values into .env, keeping unrelated keys and comments."""
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif template is not None and template.exists():
        lines = template.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    pending = dict(values)
    for i, line in enumerate(lines):
        match = ENV_KEY.match(line.strip())
        if match and match.group(1) in pending:
            lines[i] = f"{match.group(1)}={pending.pop(match.group(1))}"
    lines.extend(f"{key}={value}" for key, value in pending.items())
    # Create with owner-only permissions before any secret is written.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    if os.name == "posix":
        os.chmod(path, 0o600)


class Prompter:
    def __init__(self, interactive: bool):
        self.interactive = interactive

    def ask(self, question: str, default: str = "") -> str:
        if not self.interactive:
            return default
        suffix = f" [{default}]" if default else ""
        answer = input(f"{question}{suffix}: ").strip()
        return answer or default

    def secret(self, question: str) -> str:
        if not self.interactive:
            return ""
        return getpass.getpass(f"{question}（输入不回显，可留空稍后在 .env 填写）: ").strip()

    def confirm(self, question: str, default: bool) -> bool:
        if not self.interactive:
            return default
        hint = "Y/n" if default else "y/N"
        while True:
            answer = input(f"{question} [{hint}]: ").strip().lower()
            if not answer:
                return default
            if answer in {"y", "yes", "是"}:
                return True
            if answer in {"n", "no", "否"}:
                return False

    def choose(self, question: str, options: list[str], default: str) -> str:
        if not self.interactive:
            return default
        while True:
            answer = self.ask(f"{question}（{' / '.join(options)}）", default)
            if answer in options:
                return answer


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> bool:
    print("$ " + " ".join(command))
    return subprocess.run(command, cwd=cwd, env=env, check=False).returncode == 0


def install_pdf2zh(root: Path) -> bool:
    uv = shutil.which("uv")
    if not uv:
        print("找不到 uv，跳过 pdf2zh 安装。")
        return False
    constraints = root / "deploy" / "pdf2zh-constraints.txt"
    if not _run(
        [
            uv,
            "tool",
            "install",
            "--python",
            PDF2ZH_PYTHON,
            "--constraint",
            str(constraints),
            PDF2ZH_SPEC,
        ],
        cwd=root,
    ):
        return False
    tool_dir = subprocess.run(
        [uv, "tool", "dir"], text=True, capture_output=True, check=False
    ).stdout.strip()
    python = Path(tool_dir) / "pdf2zh" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(None, [str(root / "src"), os.environ.get("PYTHONPATH")])
        ),
    }
    return _run([str(python), "-m", "paper_collector.font_repair"], cwd=root, env=env)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-collector setup",
        description="交互式生成 config.toml 与 .env，并按需安装可选组件",
    )
    parser.add_argument("--yes", action="store_true", help="不提问，使用命令行参数和默认值")
    parser.add_argument("--force", action="store_true", help="基于示例重新生成 config.toml")
    parser.add_argument(
        "--translation",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="pdf2zh 中英双语翻译",
    )
    parser.add_argument(
        "--baidupan",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="上传百度网盘并附分享链接（仅 Server酱）",
    )
    parser.add_argument(
        "--notes", action=argparse.BooleanOptionalAction, default=None, help="HTML 阅读笔记"
    )
    parser.add_argument("--notify", choices=["serverchan", "wecom", "none"], default=None)
    parser.add_argument("--schedule", default=None, metavar="HH:MM|none", help="每日定时运行时间")
    parser.add_argument(
        "--skip-install", action="store_true", help="只写配置，不安装依赖或定时任务"
    )
    return parser


def run_setup(argv: list[str], root: Path) -> None:
    args = _parser().parse_args(argv)
    prompt = Prompter(interactive=not args.yes and sys.stdin.isatty())
    example = root / "config.example.toml"
    config_path = root / "config.toml"
    env_path = root / ".env"
    if not example.exists():
        raise RuntimeError(f"找不到 {example}；请在项目根目录运行 setup")

    print("== PaperCollector 安装向导 ==")
    print("核心功能：抓取论文 → Claude Code 精排深读。以下可选功能均可关闭。\n")

    reuse = config_path.exists() and not args.force
    text = (config_path if reuse else example).read_text(encoding="utf-8")
    if reuse:
        print(f"沿用已有 {config_path.name}，只更新所选功能开关（--force 可从示例重建）。")
    secrets: dict[str, str] = {}

    email = prompt.ask("OpenAlex 联系邮箱（用于礼貌访问）", "you@example.com" if not reuse else "")
    if email:
        text = set_toml_value(text, "profile", "email", email)

    # Agent backend: always required.
    if prompt.confirm("现在配置 Claude Code 的 Anthropic 兼容端点（如 DeepSeek）？", True):
        secrets["ANTHROPIC_BASE_URL"] = prompt.ask(
            "ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"
        )
        token = prompt.secret("ANTHROPIC_AUTH_TOKEN")
        if token:
            secrets["ANTHROPIC_AUTH_TOKEN"] = token
        model = prompt.ask("模型名", "deepseek-flash")
        text = set_toml_value(text, "agent", "model", model)

    translation = args.translation
    if translation is None:
        translation = prompt.confirm("启用 pdf2zh 中英双语翻译？（体积较大，需单独安装）", False)
    text = set_toml_value(text, "translation", "enabled", translation)
    if translation and prompt.confirm("翻译接口复用上面的 DeepSeek Key？", True):
        secrets.setdefault("OPENAILIKED_BASE_URL", "https://api.deepseek.com/v1")
        secrets.setdefault("OPENAILIKED_MODEL", "deepseek-flash")
        text = set_toml_value(text, "translation", "OPENAILIKED_API_KEY", "${ANTHROPIC_AUTH_TOKEN}")

    notify = args.notify or prompt.choose(
        "推送通道：serverchan=微信 Server酱，wecom=企业微信，none=只保存到本地",
        ["serverchan", "wecom", "none"],
        "none",
    )
    text = set_toml_value(text, "notify", "channel", notify)
    if notify == "serverchan":
        key = prompt.secret("SERVERCHAN_SENDKEY")
        if key:
            secrets["SERVERCHAN_SENDKEY"] = key
    elif notify == "wecom":
        text = set_toml_value(text, "notify", "wecom_enabled", True)
        key = prompt.secret("WECOM_WEBHOOK_KEY")
        if key:
            secrets["WECOM_WEBHOOK_KEY"] = key

    baidupan = args.baidupan
    if notify != "serverchan":
        baidupan = False
    elif baidupan is None:
        baidupan = prompt.confirm("上传百度网盘并在推送中附分享链接？（需 BaiduPCS-Go）", False)
    text = set_toml_value(text, "baidupan", "enabled", baidupan)
    if baidupan:
        cookie = prompt.secret("BAIDUPAN_COOKIE（BDUSS=...; STOKEN=...）")
        if cookie:
            secrets["BAIDUPAN_COOKIE"] = cookie

    notes = args.notes
    if notes is None:
        notes = prompt.confirm("启用 HTML 阅读笔记？（需要可访问的笔记服务地址）", False)
    text = set_toml_value(text, "reading_notes", "enabled", notes)
    if notes:
        url = prompt.ask("笔记公开访问地址 READING_NOTES_BASE_URL", "http://127.0.0.1:8765")
        secrets["READING_NOTES_BASE_URL"] = url
        if os.name == "nt":
            # The skill engine needs a filesystem sandbox only available on Linux/macOS.
            text = set_toml_value(text, "reading_notes", "engine", "json")

    schedule = args.schedule
    if schedule is None:
        schedule = (
            prompt.ask("每日定时运行时间 HH:MM", "09:00")
            if prompt.confirm("安装每日定时任务？", False)
            else "none"
        )

    config_path.write_text(text, encoding="utf-8")
    update_env(env_path, secrets, template=root / ".env.example")
    print(f"\n已写入 {config_path} 和 {env_path}（.env 仅本人可读，已被 gitignore）。")

    if not args.skip_install:
        uv = shutil.which("uv")
        if notes and uv:
            _run([uv, "sync", "--extra", "notes"], cwd=root)
        if (
            translation
            and not shutil.which("pdf2zh")
            and prompt.confirm("现在安装 pdf2zh？", True)
            and not install_pdf2zh(root)
        ):
            print("pdf2zh 安装失败，可稍后重试：uv run paper-collector setup --translation")
        if schedule != "none":
            if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", schedule):
                raise RuntimeError("定时格式应为 HH:MM，例如 09:00")
            hour, minute = (int(part) for part in schedule.split(":"))
            from paper_collector.config import load_config
            from paper_collector.scheduler import install_schedule

            try:
                where = install_schedule(load_config(config_path), hour, minute)
                print(f"已安装每日 {hour:02d}:{minute:02d} 定时任务：{where}")
            except (ValueError, RuntimeError, OSError) as exc:  # Config/secrets may be incomplete.
                print(
                    f"定时任务未安装：{exc}。补齐配置后运行 uv run paper-collector schedule install"
                )

    print("\n后续步骤：")
    if not shutil.which("claude"):
        print("- 安装 Claude Code CLI：npm install -g @anthropic-ai/claude-code")
    if baidupan and not shutil.which("BaiduPCS-Go"):
        print("- 安装 BaiduPCS-Go：https://github.com/qjfoidnh/BaiduPCS-Go/releases")
    print(f"- 编辑 {config_path.name} 中的 [[interests]]，写入你自己的研究方向")
    print("- 检查环境：uv run paper-collector doctor")
    print("- 试运行：uv run paper-collector run --dry-run")
