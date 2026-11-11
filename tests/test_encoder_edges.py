"""编码检测边界分支补充测试。"""
import codecs

from archive_extractor.core.encoder import detect_encoding


def test_unknown_declared_charset_falls_back():
    raw = b'<meta charset="unknown-encoding">hello'
    text, encoding = detect_encoding(raw)
    assert "hello" in text and encoding == "utf-8"


def test_control_characters_use_fallback():
    text, encoding = detect_encoding(b"a\x00b\x01c")
    assert "a" in text and encoding in {"utf-8", "gb18030", "big5"}


def test_utf16_bom():
    raw = codecs.BOM_UTF16_LE + "表单".encode("utf-16-le")
    text, encoding = detect_encoding(raw)
    assert text == "表单" and encoding == "utf-16"
