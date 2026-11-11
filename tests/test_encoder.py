"""编码识别与安全读取测试。"""
import codecs

import pytest

from archive_extractor.core.encoder import detect_encoding, read_html
from archive_extractor.utils.common import EncodingError


def test_utf8_and_bom():
    text, encoding = detect_encoding("中文表单".encode("utf-8"))
    assert text == "中文表单" and encoding == "utf-8"
    text, encoding = detect_encoding(codecs.BOM_UTF8 + "中文".encode("utf-8"))
    assert text == "中文" and encoding == "utf-8-sig"
    assert detect_encoding(b"") == ("", "utf-8")


def test_gbk_and_declared_big5():
    text, encoding = detect_encoding("企业申报表".encode("gbk"))
    assert text == "企业申报表" and encoding in {"gb18030", "gb2312"}
    big5 = '<meta charset="big5">繁體中文測試'.encode("big5")
    text, encoding = detect_encoding(big5)
    assert encoding == "big5" and "繁體中文" in text


def test_html_meta_charset_is_used():
    raw = '<meta http-equiv="Content-Type" content="text/html; charset=gbk">企业'.encode(
        "gbk"
    )
    text, encoding = detect_encoding(raw)
    assert encoding in {"gbk", "gb18030"} and text.endswith("企业")


def test_broken_bytes_raise():
    with pytest.raises(EncodingError):
        detect_encoding(b"\x80\x81\xfe\xff\x80")


def test_read_html_and_limits(tmp_path):
    path = tmp_path / "a.html"
    path.write_text("测试", encoding="utf-8")
    text, encoding = read_html(path)
    assert text == "测试" and encoding == "utf-8"
    with pytest.raises(EncodingError):
        read_html(path, max_bytes=1)
    with pytest.raises(EncodingError):
        read_html(tmp_path / "missing.html")
