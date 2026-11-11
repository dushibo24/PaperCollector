from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from paper_collector import __version__
from paper_collector.config import ConfigError, load_config
from paper_collector.doctor import run_checks
from paper_collector.notes_server import serve_notes
from paper_collector.pipeline import Pipeline, PipelineBusyError
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
        ("check-delivery", "独立核查当天是否完整投递，未完成时告警"),
        ("doctor", "检查依赖、网络和配置"),
    ]:
        item = sub.add_parser(command, help=help_text)
        item.add_argument("--config", default="config.toml")
        if command == "run":
            item.add_argument(
                "--dry-run", action="store_true", help="抓取并精排，但不下载、翻译或推送"
            )
            item.add_argument(
                "--scheduled", action="store_true", help="定时模式：当天成功投递后不重复发送"
            )
            item.add_argument("--no-notify", action="store_true", help="生成全部产物，但不发送微信")
            item.add_argument(
                "--skip-discovery", action="store_true", help="使用本地候选池重试，不重复抓取来源"
            )

    notes_server = sub.add_parser("serve-notes", help="仅提供已发布阅读笔记的 HTTP 服务")
    notes_server.add_argument("--config", default="config.toml")
    write_note = sub.add_parser("write-note", help="为已深读的论文补写 HTML 笔记，不发送通知")
    write_note.add_argument("paper_id")
    write_note.add_argument("--config", default="config.toml")

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
        if args.command == "serve-notes":
            serve_notes(config)
            return
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
            if args.command == "check-delivery":
                if pipeline.check_daily_delivery():
                    print("已核查：今日论文已完整投递。")
                else:
                    print("今日投递尚未完成，已尝试发送告警。")
                    raise SystemExit(1)
            elif args.command == "discover":
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
                selected = pipeline.run(
                    dry_run=args.dry_run,
                    no_notify=args.no_notify,
                    skip_discovery=args.skip_discovery,
                    scheduled=args.scheduled,
                )
                if selected:
                    print(f"本次处理：{selected}")
                elif args.scheduled and pipeline.db.delivered_on(
                    datetime.now(ZoneInfo("Asia/Shanghai")).date()
                ):
                    print("今日论文已投递，未重复发送。")
                else:
                    print("本次没有达到推荐门槛且产物准备完整的论文。")
            elif args.command == "write-note":
                print(f"阅读笔记：{pipeline.write_reading_note(args.paper_id)}")
        finally:
            pipeline.close()
    except PipelineBusyError as exc:
        if getattr(args, "scheduled", False):
            print("已有任务在运行，本次定时恢复检查跳过。")
            return
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except (ConfigError, RuntimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
