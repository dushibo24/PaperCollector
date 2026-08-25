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
