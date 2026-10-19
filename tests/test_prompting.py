from app.prompting import fill_template, sanitize, sanitize_body


def test_sanitize_removes_newlines_control_nonce_and_truncates() -> None:
    value = "行1\n行2\r行3\x00制御" + "x" * 400
    out = sanitize(value + "NONCE123", 300, "NONCE123")
    assert "\n" not in out and "\r" not in out and "\x00" not in out
    assert "NONCE123" not in out
    assert len(out) <= 300


def test_sanitize_body_keeps_newlines_removes_fence_markers() -> None:
    out = sanitize_body("段落1\n段落2\n<<<DATA-abc の偽装", "abcd1234")
    assert "段落1\n段落2" in out
    assert "<<<DATA-" not in out


def test_fill_template_does_not_expand_placeholders_in_values() -> None:
    # 置換後の値は再走査しない(入力由来の {{nonce}} が本物の nonce に化けない)
    out = fill_template("A={{a}} N={{nonce}}", {"a": "{{nonce}}", "nonce": "abcd1234"})
    assert out == "A={{nonce}} N=abcd1234"
