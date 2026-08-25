from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from paper_collector import __version__
from paper_collector.config import ConfigError, load_config
from paper_collector.doctor import run_checks
from paper_collector.pipeline import Pipeline
from paper_collector.scheduler import install_launchd, uninstall_launchd


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-collector", description="每天深读并推送一篇值得看的计算机论文"
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="生成 config.toml")
    init.add_argument("--force", action="store_true", help="覆盖已有配置")

    for command, help_text in [
        ("run", "运行完整流水线"),
        ("discover", "只抓取候选并写入数据库"),
        ("status", "查看本地状态"),
        ("doctor", "检查依赖、网络和配置"),
    ]:
        item = sub.add_parser(command, help=help_text)
        item.add_argument("--config", default="config.toml")
        if command == "run":
            item.add_argument(
                "--dry-run", action="store_true", help="抓取并精排，但不下载、翻译或推送"
            )
            item.add_argument("--no-notify", action="store_true", help="生成全部产物，但不发送微信")

    schedule = sub.add_parser("schedule", help="管理 macOS 每日定时任务")
    schedule.add_argument("action", choices=["install", "uninstall"])
    schedule.add_argument("--config", default="config.toml")
    schedule.add_argument("--hour", type=int, default=9)
    schedule.add_argument("--minute", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path.cwd()
    try:
        if args.command == "init":
            source = root / "config.example.toml"
            target = root / "config.toml"
            if target.exists() and not args.force:
                raise RuntimeError("config.toml 已存在；确需覆盖请加 --force")
            shutil.copy2(source, target)
            print(f"已生成 {target}，请先填写邮箱和兴趣，并在 .env 配置 SERVERCHAN_SENDKEY。")
            return
        if args.command == "schedule" and args.action == "uninstall":
            print(f"已移除 {uninstall_launchd()}")
            return

        config = load_config(args.config)
        if args.command == "doctor":
            checks = run_checks(config)
            for check in checks:
                print(f"{'✓' if check.ok else '✗'} {check.name}: {check.detail}")
            if not all(check.ok for check in checks):
                raise SystemExit(1)
            return
        if args.command == "schedule":
            print(
                f"已安装每日 {args.hour:02d}:{args.minute:02d} 任务：{install_launchd(config, args.hour, args.minute)}"
            )
            return

        pipeline = Pipeline(config)
        try:
            if args.command == "discover":
                papers = pipeline.discover()
                print(f"发现并更新 {len(papers)} 篇候选；数据库：{pipeline.db.path}")
            elif args.command == "status":
                stats = pipeline.db.stats()
                print(
                    "论文状态："
                    + ("，".join(f"{key}={value}" for key, value in stats.items()) or "暂无记录")
                )
                print(f"数据库：{pipeline.db.path}")
            elif args.command == "run":
                selected = pipeline.run(dry_run=args.dry_run, no_notify=args.no_notify)
                if selected:
                    print(f"本次处理：{selected}")
                else:
                    print("本次没有达到推荐门槛的论文。")
        finally:
            pipeline.close()
    except (ConfigError, RuntimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
