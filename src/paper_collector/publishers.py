from __future__ import annotations

import hashlib
import posixpath
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from paper_collector.config import AppConfig


class PublishError(RuntimeError):
    pass


@dataclass(slots=True)
class ShareLink:
    remote_path: str
    url: str
    password: str


SHARE_LINK_PATTERN = re.compile(r"链接:\s*(https?://\S+)")
PASSWORD_PATTERN = re.compile(r"(?:密码|提取码):\s*([A-Za-z0-9]{4})")


def deterministic_password(value: str) -> str:
    alphabet = "23456789abcdefghjkmnpqrstuvwxyz"
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return "".join(alphabet[byte % len(alphabet)] for byte in digest[:4])


def parse_share_output(output: str, remote_path: str, fallback_password: str) -> ShareLink:
    match = SHARE_LINK_PATTERN.search(output)
    if not match:
        raise PublishError(f"BaiduPCS-Go 未返回分享链接：{output[-1200:]}")
    url = match.group(1).rstrip("，,。")
    password_match = PASSWORD_PATTERN.search(output)
    password = password_match.group(1) if password_match else fallback_password
    if "?pwd=" in url:
        password = url.split("?pwd=", 1)[1].split("&", 1)[0] or password
    return ShareLink(remote_path=remote_path, url=url, password=password)


class BaiduPCSGoPublisher:
    def __init__(self, config: AppConfig):
        self.config = config
        self.executable = shutil.which(config.baidupcs_bin)
        if not self.executable:
            raise PublishError(
                f"找不到 {config.baidupcs_bin}；macOS 可运行 brew install baidupcs-go"
            )

    @staticmethod
    def _failed(output: str) -> bool:
        lowered = output.casefold()
        markers = ("失败", "未登录", "请登录", "error:", "panic:", "登录百度帐号")
        return any(marker.casefold() in lowered for marker in markers)

    def _run(self, args: list[str], *, allow_failure: bool = False) -> str:
        try:
            completed = subprocess.run(
                [self.executable, *args],
                text=True,
                capture_output=True,
                timeout=self.config.baidupcs_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PublishError(
                f"BaiduPCS-Go 超时（{self.config.baidupcs_timeout_seconds}s）"
            ) from exc
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
        failed = completed.returncode != 0 or self._failed(output)
        if failed and not allow_failure:
            raise PublishError(f"BaiduPCS-Go 执行失败：{output[-2000:]}")
        return output

    def check_login(self) -> tuple[bool, str]:
        output = self._run(["who"], allow_failure=True)
        not_logged_in = bool(re.search(r"uid:\s*0(?:\D|$)", output)) or "用户名: ," in output
        if not output or self._failed(output) or not_logged_in:
            return False, output or "未检测到已登录账号"
        return True, output.splitlines()[0]

    def publish(self, local_path: Path) -> ShareLink:
        if not local_path.is_file():
            raise PublishError(f"待上传文件不存在：{local_path}")
        logged_in, detail = self.check_login()
        if not logged_in:
            raise PublishError(f"BaiduPCS-Go 尚未登录：{detail}")
        remote_dir = "/" + self.config.baidupcs_remote_dir.strip("/")
        remote_path = posixpath.join(remote_dir, local_path.name)
        # mkdir reports an error when the directory already exists; upload verification is authoritative.
        self._run(["mkdir", remote_dir], allow_failure=True)
        self._run(["upload", str(local_path.resolve()), remote_dir, "--policy", "overwrite"])
        metadata = self._run(["meta", remote_path])
        if posixpath.basename(remote_path) not in metadata:
            raise PublishError(f"上传后无法在百度网盘确认文件：{remote_path}")

        password = self.config.baidupcs_share_password or deterministic_password(remote_path)
        output = self._run(
            [
                "share",
                "set",
                remote_path,
                "-p",
                password,
                "--period",
                str(self.config.baidupcs_share_period_days),
                "-f",
            ]
        )
        return parse_share_output(output, remote_path, password)
