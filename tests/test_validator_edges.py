"""校验器边界分支补充测试。"""
from archive_extractor.core.validator import register_validator, validate_data


def test_invalid_numeric_compare_and_unknown_validator():
    rule = {
        "fields": {
            "a": {"type": "text", "validators": [
                {"type": "unknown_type", "level": "info"},
                {"type": "range", "min": 1, "message": "范围"},
            ]},
        },
        "validations": [
            {"type": "compare", "left": "a", "op": ">", "right": 1,
             "message": "比较"},
            {"type": "compare", "left": "a", "op": "==", "right": "x",
             "message": "相等"},
        ],
    }
    issues = validate_data(rule, {"a": "not-number"})
    assert [item.validator for item in issues] == ["range", "compare"]
    assert issues[1].message == "相等"


def test_object_non_dict_and_nested_object():
    fields = {
        "outer": {
            "type": "object", "required": True,
            "fields": {
                "inner": {
                    "type": "object", "required": True,
                    "fields": {"value": {"type": "text", "required": True}},
                }
            },
        }
    }
    assert validate_data({"fields": fields}, {"outer": "bad"}) == []
    issues = validate_data({"fields": fields}, {"outer": {"inner": None}})
    assert issues[0].field == "outer.inner"


def test_custom_validator_message_string_and_bad_regex():
    @register_validator("always_message")
    def always(value, data, config):
        return "固定提示"

    rule = {
        "fields": {"x": {"type": "text", "validators": [
            {"type": "always_message", "level": "info"},
            {"type": "regex", "pattern": "[", "message": "无效正则"},
            {"type": "enum", "values": [], "message": "枚举"},
        ]}},
        "validations": [],
    }
    issues = validate_data(rule, {"x": "v"})
    assert [item.validator for item in issues] == [
        "always_message", "regex", "enum"
    ]
    assert issues[0].level == "info"


def test_cross_validation_missing_and_invalid_compare():
    validations = [
        {"type": "date_order", "start": "a", "end": "b", "message": "日期"},
        {"type": "compare", "left": "a", "op": ">", "right": "b",
         "message": "比较"},
    ]
    assert validate_data({"fields": {}, "validations": validations},
                         {"a": None, "b": "2024-01-01"}) == []
    assert validate_data({"fields": {}, "validations": validations},
                         {"a": 1, "b": "x"}) == []
