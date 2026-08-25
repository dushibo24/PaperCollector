from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class NotifyError(RuntimeError):
    pass


class ServerChanNotifier:
    def __init__(self, sendkey: str):
        if not sendkey or not sendkey.startswith("SCT"):
            raise NotifyError("Server酱 SendKey 为空或格式不正确")
        self.endpoint = f"https://sctapi.ftqq.com/{urllib.parse.quote(sendkey, safe='')}.send"

    def send(self, title: str, content: str) -> None:
        body = urllib.parse.urlencode(
            {"title": title[:128], "desp": content, "short": title[:64]}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise NotifyError(f"Server酱请求失败：{exc}") from exc
        data = result.get("data") or {}
        errno = data.get("errno", 0) if isinstance(data, dict) else 0
        try:
            errno = int(errno)
        except (TypeError, ValueError):
            errno = -1
        pushid = data.get("pushid") if isinstance(data, dict) else None
        if result.get("code") != 0 or errno != 0 or not pushid:
            raise NotifyError(f"Server酱返回错误：{result}")


class WeComNotifier:
    def __init__(self, webhook_key: str):
        if "key=" in webhook_key:
            webhook_key = urllib.parse.parse_qs(urllib.parse.urlparse(webhook_key).query).get(
                "key", [""]
            )[0]
        if not webhook_key:
            raise NotifyError("企业微信机器人 key 为空")
        self.key = webhook_key
        self.webhook_url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={urllib.parse.quote(webhook_key)}"
        self.upload_url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key={urllib.parse.quote(webhook_key)}&type=file"

    @staticmethod
    def _post_json(url: str, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise NotifyError(f"企业微信请求失败：{exc}") from exc
        if result.get("errcode") != 0:
            raise NotifyError(f"企业微信返回错误：{result}")
        return result

    def send_markdown(self, content: str) -> None:
        encoded = content.encode("utf-8")
        if len(encoded) > 4096:
            # Avoid cutting in the middle of a UTF-8 sequence.
            encoded = encoded[:4080]
            content = encoded.decode("utf-8", errors="ignore") + "…"
        self._post_json(self.webhook_url, {"msgtype": "markdown", "markdown": {"content": content}})

    def upload_file(self, path: Path) -> str:
        size = path.stat().st_size
        if size > 20 * 1024 * 1024:
            raise NotifyError(f"企业微信机器人文件上限 20MB，当前 {size / 1024 / 1024:.1f}MB")
        boundary = "----PaperCollector" + uuid.uuid4().hex
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="media"; filename="{path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        body = prefix + path.read_bytes() + suffix
        request = urllib.request.Request(
            self.upload_url,
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise NotifyError(f"企业微信文件上传失败：{exc}") from exc
        if result.get("errcode") != 0 or not result.get("media_id"):
            raise NotifyError(f"企业微信文件上传返回错误：{result}")
        return str(result["media_id"])

    def send_file(self, path: Path) -> None:
        media_id = self.upload_file(path)
        self._post_json(self.webhook_url, {"msgtype": "file", "file": {"media_id": media_id}})
