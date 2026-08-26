import subprocess
from dataclasses import replace

import pytest

from paper_collector.publishers import (
    BaiduPCSGoPublisher,
    PublishError,
    deterministic_password,
    parse_share_output,
)


def test_parse_combined_baidupan_share_link():
    result = parse_share_output(
        "shareID: 123, 链接: https://pan.baidu.com/s/abc?pwd=k9m2\n",
        "/PaperCollector/paper.pdf",
        "xxxx",
    )
    assert result.url == "https://pan.baidu.com/s/abc?pwd=k9m2"
    assert result.password == "k9m2"
    assert result.remote_path == "/PaperCollector/paper.pdf"


def test_parse_separate_password_and_deterministic_fallback():
    result = parse_share_output(
        "shareID: 123, 链接: https://pan.baidu.com/s/abc, 密码: 2abc",
        "/PaperCollector/paper.pdf",
        "xxxx",
    )
    assert result.password == "2abc"
    assert len(deterministic_password(result.remote_path)) == 4
    assert deterministic_password(result.remote_path) == deterministic_password(result.remote_path)


def test_missing_share_link_is_an_error():
    with pytest.raises(PublishError, match="未返回分享链接"):
        parse_share_output("设置分享失败", "/x.pdf", "xxxx")


def test_uid_zero_is_not_treated_as_logged_in(config, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/fake/BaiduPCS-Go")
    publisher = BaiduPCSGoPublisher(config)
    monkeypatch.setattr(
        publisher,
        "_run",
        lambda _args, allow_failure=False: "当前帐号 uid: 0, 用户名: , 性别: , 年龄: 0.0",
    )
    ok, _ = publisher.check_login()
    assert ok is False


def test_nonzero_uid_is_logged_in_even_when_username_is_empty(config, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/fake/BaiduPCS-Go")
    publisher = BaiduPCSGoPublisher(config)
    monkeypatch.setattr(
        publisher,
        "_run",
        lambda _args, allow_failure=False: "当前帐号 uid: 123, 用户名: , 性别: ♂",
    )

    ok, _ = publisher.check_login()

    assert ok is True


def test_cookie_login_is_automatic_and_verified(config, monkeypatch):
    cookie = "BDUSS=x; STOKEN=y"
    config = replace(config, baidupcs_cookie=cookie)
    monkeypatch.setattr("shutil.which", lambda _name: "/fake/BaiduPCS-Go")
    publisher = BaiduPCSGoPublisher(config)
    calls: list[list[str]] = []
    who_outputs = iter(
        [
            "当前帐号 uid: 0, 用户名: , 性别: , 年龄: 0.0",
            "当前帐号 uid: 123, 用户名: test-user",
        ]
    )

    def fake_run(args, *, allow_failure=False, sensitive_values=()):
        calls.append(args)
        if args == ["who"]:
            return next(who_outputs)
        assert args == ["login", f"-cookies={cookie}"]
        assert sensitive_values == (cookie,)
        return "登录成功"

    monkeypatch.setattr(publisher, "_run", fake_run)

    assert "uid: 123" in publisher.ensure_login()
    assert calls == [["who"], ["login", f"-cookies={cookie}"], ["who"]]


def test_cookie_login_is_skipped_when_session_is_valid(config, monkeypatch):
    config = replace(config, baidupcs_cookie="BDUSS=x")
    monkeypatch.setattr("shutil.which", lambda _name: "/fake/BaiduPCS-Go")
    publisher = BaiduPCSGoPublisher(config)
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return "当前帐号 uid: 123, 用户名: test-user"

    monkeypatch.setattr(publisher, "_run", fake_run)

    publisher.ensure_login()
    assert calls == [["who"]]


def test_cookie_is_redacted_from_cli_errors(config, monkeypatch):
    cookie = "BDUSS=secret"
    monkeypatch.setattr("shutil.which", lambda _name: "/fake/BaiduPCS-Go")
    publisher = BaiduPCSGoPublisher(config)
    monkeypatch.setattr(
        "paper_collector.publishers.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout=f"登录失败：{cookie}", stderr=""
        ),
    )

    with pytest.raises(PublishError) as raised:
        publisher._run(
            ["login", f"-cookies={cookie}"],
            sensitive_values=(cookie,),
        )

    assert cookie not in str(raised.value)
    assert "[redacted]" in str(raised.value)


def test_baidupan_only_uploads_zip_packages(config, tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/fake/BaiduPCS-Go")
    publisher = BaiduPCSGoPublisher(config)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")

    with pytest.raises(PublishError, match="ZIP"):
        publisher.publish(pdf)
