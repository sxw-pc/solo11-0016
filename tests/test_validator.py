"""字段校验、跨字段校验与扩展机制测试。"""
from archive_extractor.core.validator import (
    ValidationIssue,
    highest_level,
    issues_to_dicts,
    register_validator,
    validate_data,
)


def rule_with(fields, validations=None):
    return {
        "fields": fields,
        "validations": validations or [],
    }


def test_required_length_enum_and_patterns():
    field = {
        "type": "text",
        "required": True,
        "label": "手机号",
        "validators": [
            {"type": "min_length", "value": 11, "message": "太短"},
            {"type": "max_length", "value": 11, "message": "太长"},
            {"type": "phone", "message": "手机号错误"},
        ],
    }
    issues = validate_data(rule_with({"phone": field}), {"phone": "123"})
    assert [item.validator for item in issues] == ["min_length", "phone"]
    issues = validate_data(rule_with({"phone": field}), {"phone": ""})
    assert issues[0].validator == "required"
    assert validate_data(rule_with({"phone": field}), {"phone": "13800000000"}) == []


def test_enum_range_and_builtin_patterns():
    fields = {
        "kind": {
            "type": "enum", "validators": [
                {"type": "enum", "values": ["A", "B"], "message": "枚举错误"}
            ]
        },
        "amount": {
            "type": "number", "validators": [
                {"type": "range", "min": 0, "max": 10, "message": "范围错误"}
            ]
        },
        "credit": {
            "type": "text", "validators": [
                {"type": "credit_code", "message": "信用代码错误"}
            ]
        },
        "email": {
            "type": "text", "validators": [
                {"type": "email", "message": "邮箱错误"}
            ]
        },
        "ident": {
            "type": "text", "validators": [
                {"type": "id_card", "message": "证件错误"}
            ]
        },
    }
    data = {"kind": "C", "amount": 11, "credit": "X", "email": "bad",
            "ident": "123"}
    issues = validate_data(rule_with(fields), data)
    assert {item.field for item in issues} == {
        "kind", "amount", "credit", "email", "ident"
    }
    assert validate_data(rule_with(fields), {
        "kind": "A", "amount": 2, "credit": "91110000MA00000001",
        "email": "demo@example.test", "ident": "110101199001011234",
    }) == []


def test_regex_invalid_value_and_config():
    fields = {"code": {"type": "text", "validators": [
        {"type": "regex", "pattern": r"^[A-Z]{2}-\d{2}$", "message": "格式错误",
         "level": "warning"}
    ]}}
    issues = validate_data(rule_with(fields), {"code": "bad"})
    assert issues[0].level == "warning"
    fields["code"]["validators"][0]["pattern"] = "["
    assert validate_data(rule_with(fields), {"code": "bad"})[0].validator == "regex"


def test_cross_field_date_order_and_compare():
    fields = {
        "start": {"type": "date"}, "end": {"type": "date"},
        "left": {"type": "number"}, "right": {"type": "number"},
    }
    validations = [
        {"type": "date_order", "start": "start", "end": "end",
         "level": "error", "message": "结束早于开始"},
        {"type": "compare", "left": "left", "op": ">", "right": 5,
         "level": "warning", "message": "数量不足"},
        {"type": "compare", "left": "left", "op": "<", "right_field": "right",
         "level": "info", "message": "左右关系"},
    ]
    issues = validate_data(
        rule_with(fields, validations),
        {"start": "2024-02-01", "end": "2024-01-01", "left": 3, "right": 2},
    )
    assert {item.level for item in issues} == {"error", "warning", "info"}
    assert len(issues) == 3
    assert validate_data(
        rule_with(fields, validations),
        {"start": "2024-01-01", "end": "2024-02-01", "left": 6, "right": 9},
    ) == []


def test_object_required_and_nested_validators():
    fields = {
        "contact": {
            "type": "object", "required": True, "label": "联系人",
            "fields": {
                "name": {"type": "text", "required": True, "label": "姓名"},
                "email": {"type": "text", "validators": [
                    {"type": "email", "message": "邮箱错误"}
                ]},
            },
        }
    }
    issues = validate_data(rule_with(fields), {"contact": None})
    assert issues[0].field == "contact"
    issues = validate_data(
        rule_with(fields), {"contact": {"name": "", "email": "bad"}}
    )
    assert {item.field for item in issues} == {"contact.name", "contact.email"}


def test_custom_validator_level_tuple_and_helpers():
    @register_validator("even_number")
    def even(value, data, config):
        if value % 2:
            return "warning", "必须是偶数"
        return None

    fields = {"number": {"type": "number", "validators": [
        {"type": "even_number", "level": "error"}
    ]}}
    issues = validate_data(rule_with(fields), {"number": 3})
    assert issues[0].level == "warning" and issues[0].validator == "even_number"
    assert highest_level(issues) == "warning"
    assert highest_level([]) == ""
    assert issues_to_dicts(issues)[0]["message"] == "必须是偶数"
    assert ValidationIssue("info", "x", "y").to_dict()["field"] == "x"


def test_invalid_dates_are_skipped_in_cross_validation():
    validations = [
        {"type": "date_order", "start": "a", "end": "b", "message": "日期错误"}
    ]
    assert validate_data(rule_with({}, validations), {"a": "bad", "b": "bad"}) == []
