# YAML 提取规则 Schema

规则目录中的每个 `.yaml` / `.yml` 文件定义一类表单。加载时先做结构校验，再计算内容摘要，生成有效版本。规则目录通过 `--rules-dir` 指定；不指定时使用内置 `archive_extractor/config/default_rules/`。

## 一、顶层结构

```yaml
rule_id: declare_form        # 必填，^[a-z][a-z0-9_]{1,63}$
name: 企业申报表              # 必填
version: 1.0.0               # 必填，业务版本
form_type: declare           # 可选，默认等于 rule_id
description: 字段说明         # 可选
match:                       # 必填
  form_selector: "form#declareForm"
  marker: 'data-form-type="declare"'
fields:                      # 必填，非空映射
  ...
dedup:                       # 可选
  primary_key: [declare_no]
  fuzzy_fields: [applicant, project_name]
  threshold: 0.97
validations:                 # 可选，跨字段校验
  - type: compare
    left: amount
    op: ">"
    right: 0
    level: info
    message: 金额为零时建议人工复核
```

匹配顺序：按文件名排序加载规则，逐条尝试 `form_selector`；未命中再看 `marker` 是否出现在原始 HTML 文本中。`form_selector` 或 `marker` 至少配置一个。

## 二、字段定义

每个字段必须包含 `selector` 或 `default` 之一。

| 键 | 类型 | 默认 | 说明 |
|----|------|------|------|
| `label` | string | 字段名 | 校验消息中的中文名 |
| `selector` | string | — | CSS/XPath/正则表达式 |
| `selector_type` | `css`/`xpath`/`regex` | `css` | 提取方式 |
| `attr` | string | — | 取节点属性，如表单控件取 `value` |
| `type` | string | `text` | `text`/`number`/`date`/`boolean`/`enum`/`object`/`array` |
| `item_type` | string | `text` | 数组元素类型，不能为 `object` |
| `all` | bool | `false` | 是否取全部命中节点；数组类型自动为真 |
| `group` | int | `0` | 正则捕获组 |
| `default` | any | — | 未命中或空值时使用；无选择器时直接返回 |
| `required` | bool | `false` | 必填校验 |
| `trim` | bool | `true` | 压缩连续空白并去首尾 |
| `remove_whitespace` | bool | `false` | 删除所有空白 |
| `replacements` | list | `[]` | 正则替换：`{pattern, to}` |
| `validators` | list | `[]` | 字段级校验 |
| `fields` | mapping | — | 仅 `object` 使用 |

### 字段类型

- `text` / `enum`：字符串。
- `number`：允许千分位；能转为整数时返回整数，否则返回浮点数；失败记为字段错误。
- `date`：依次尝试 `YYYY-MM-DD`、`YYYY/MM/DD`、`YYYY.MM.DD`、`YYYY年MM月DD日`，再用 dateutil 兜底，输出 `YYYY-MM-DD`。
- `boolean`：真值包括 `1/true/yes/y/on/是/有/checked`；假值包括 `0/false/no/n/off/否/无/空`；其他值记为错误。
- `array`：遍历全部命中节点，逐个按 `item_type` 转换；未命中返回 `default` 或 `null`。
- `object`：先定位对象容器，再在容器内按子字段 `selector` 提取。**仅支持一层嵌套**，子字段不能再是 `object`。

### 表单控件取值

- `input`：通常配置 `attr: value`；隐藏字段同理。
- `select`：不配置 `attr` 时，优先取 `option[selected]` 的 `value`，无选中项取第一个选项。
- `textarea`：取文本内容。
- 单选/复选：用 `:checked` 选择器配合 `attr: value`；复选数组使用 `all: true`。
- 嵌套表格：用容器选择器定义 `object`，子字段使用相对选择器。

### 三种提取示例

```yaml
# CSS
applicant:
  selector: '#declareForm .applicant'
  type: text

# XPath
title:
  selector: '//table[@id="detail"]//tr[2]/td[3]'
  selector_type: xpath
  type: text

# 正则，取第 1 个捕获组，取全部匹配
codes:
  selector: '编号[:：]\s*([A-Z0-9-]+)'
  selector_type: regex
  group: 1
  type: array
  all: true
```

## 三、校验器

字段级 `validators` 每项包含 `type`、可选 `level`（`error`/`warning`/`info`，默认 `error`）、`message`。

| type | 参数 | 说明 |
|------|------|------|
| `required` | — | 必填（通常直接用字段 `required: true`） |
| `min_length` / `max_length` | `value` | 字符长度 |
| `regex` | `pattern` | 正则匹配 |
| `enum` | `values` | 值必须在列表内 |
| `range` | `min`/`max` | 数值范围 |
| `phone` | — | 中国大陆手机号 |
| `id_card` | — | 18 位身份证格式 |
| `credit_code` | — | 18 位统一社会信用代码 |
| `email` | — | 邮箱格式 |
| `date` | — | `YYYY-MM-DD` 格式 |
| 自定义 | 任意 | 通过 `register_validator` 注册 |

自定义校验器：

```python
from archive_extractor.core.validator import register_validator

@register_validator("even_number")
def even_number(value, data, config):
    if value % 2:
        return "warning", "必须是偶数"   # (等级, 消息)
    return None
```

返回字符串时使用配置的等级；返回 `(level, message)` 可覆盖等级；返回假值表示通过。

## 四、跨字段校验

```yaml
validations:
  - type: date_order
    start: start_date
    end: end_date
    level: error
    message: 结束日期不得早于开始日期
  - type: compare
    left: amount
    op: ">"
    right: 0
    level: info
    message: 金额为零时建议复核
  - type: compare
    left: min_value
    op: "<="
    right_field: max_value
    level: warning
    message: 最小值不应大于最大值
```

- `op` 支持 `>`、`>=`、`<`、`<=`、`==`、`!=`。
- 任一操作数为空、无法转为数值或日期无效时跳过该条校验，不误报。
- 字段路径支持点号，如 `contact.email`（跨字段比较使用顶层键）。

## 五、字段联动（派生与约束）

规则可在顶层声明 `derivations`（字段派生）与 `constraints`（跨字段约束）。两者共同构成字段依赖图，在**规则加载时**完成校验与环检测，非法配置直接报错，不进入运行期。

### 派生字段 `derivations`

派生字段的值不由选择器抽取，而由上游字段经表达式计算得到。派生字段必须在 `fields` 中以纯字段声明（只写 `label`/`type`，**不能**配置 `selector` 或 `default`）。

```yaml
fields:
  apply_date: {label: 申报日期, selector: 'input[name="d"]', attr: value, type: date}
  apply_year: {label: 申报年份, type: text}        # 纯声明，无 selector/default
  record_no:  {label: 备案编号, type: text}

derivations:
  - field: apply_year
    expression: {type: slice, field: apply_date, start: 0, end: 4}
    on_failure: warning
  - field: record_no
    expression:
      type: concat
      parts: [{field: district_code}, {field: apply_year}, {field: serial_no}]
      separator: '-'
    on_failure: warning
```

- `field`：派生目标字段名，必须在 `fields` 中声明，且不得重复声明。
- `expression`：见下表，引用字段必须是 `fields` 中已声明的顶层字段（暂不支持 `a.b` 点号路径）。
- `on_failure`：降级时写入校验记录的等级，`error`/`warning`/`info`，默认 `warning`。

派生字段可以引用其他派生字段，形成依赖链；加载时按拓扑序求值，**存在环（含自环）时规则加载直接报错**。

### 表达式类型

| type | 参数 | 说明 |
|------|------|------|
| `concat` | `parts`、`separator` | 按序拼接；`parts` 元素为字符串字面量或 `{field: 名}` |
| `slice` | `field`、`start`、`end` | 字符串切片，如取日期前 4 位年份 |
| `case` | `field`、`branches`、`default` | 条件映射；`branches` 每项 `{equals: x, value: y}` 或 `{pattern: 正则, value: y}`，首命中生效，必须配 `default` |
| `phone_kind` | `field`、`mobile`/`landline`/`unknown` | 固话/手机分类，输出可自定义标签 |
| `coalesce` | `inputs` | 返回第一个非空输入 |
| `date_diff` | `start`、`end` | 两个日期字段的天数差（end − start） |

表达式求值不使用 `eval`/`exec`，全部为内置白名单纯函数，无法访问环境或文件系统。

### 失败降级

- 上游字段**抽取失败**（类型转换错误等）→ 派生字段按**失败路径**降级，置空并记录。
- 上游字段**为空**（未命中、空串）→ 派生字段按**空值路径**降级，置空并记录。
- 降级沿依赖链级联（A 失败 → 依赖 A 的 B 也降级），但**无关链路照常计算**。
- 派生字段绝不会拿空值/失败值算出脏结果；每次降级写入 `validations`（`validator=linkage_derivation`）并在溯源索引留痕。

### 跨字段约束 `constraints`

```yaml
constraints:
  - {type: date_order, start: register_date, end: apply_date, level: error,
     message: 申报日期不能早于登记日期}
  - {type: compare, left: amount, op: '>', right: 0, level: info,
     message: 金额为零时建议复核}
```

- 与旧 `validations` 段语义一致，但纳入依赖图：约束引用的字段若抽取失败或为空，该约束**跳过**（不误报），并在溯源索引记 `skipped`。
- 违反约束写入 `validations`（`validator=linkage_constraint`），等级可配。
- 旧 `validations` 段继续兼容，两者并存，输出汇入同一校验记录。

### 联动溯源与局部重算

- 派生字段在 `traces` 中记录：上游字段列表、表达式、联动状态（`ok`/`failed`/`degraded`）、降级原因、规则版本，与普通字段溯源同表同口径。
- `python -m archive_extractor.cli rederive <任务ID> <相对路径> --field <变化字段>`：不重解析源文件，仅按依赖图重算受影响字段的下游派生字段与相关约束，生成新结果版本，未受影响字段与溯源保持不动。

## 六、去重配置

```yaml
dedup:
  primary_key: [declare_no]
  fuzzy_fields: [applicant, project_name]
  threshold: 0.97
```

- `primary_key`：精确匹配字段列表，全部相等即判重。支持点号路径。
- `fuzzy_fields`：逐字段计算归一化编辑距离，取最小值作为整体相似度；为空则只做精确去重。
- `threshold`：0–1，默认 0.9；建议 0.95 以上，避免编号相近的不同表单被误判。
- 保留规则：源文件 `mtime` 较新者保留，另一条逻辑删除并生成报告。

## 七、加载与错误

- 同一目录内 `rule_id` 重复会直接报错。
- 非法字段名、未知类型、未知选择器类型、缺少 selector/default、非法正则、非法等级、阈值越界都会在加载阶段报错，并带文件名与字段路径。
- 有效版本格式为 `version+content_hash[:8]`，修改任意规则内容都会改变版本，历史结果仍保留旧版本字符串。
- 规则加载失败时任务标记 `failed`，不会以空规则继续运行。
