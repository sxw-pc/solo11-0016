"""从本地目录加载 YAML 规则并进行版本签名。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional, Union

import yaml

from archive_extractor.config.schema import validate_rule
from archive_extractor.utils.common import RuleError, utc_now

PathLike = Union[str, Path]
DEFAULT_RULE_DIR = Path(__file__).resolve().parent / "default_rules"


class RuleSet:
    """一组已校验规则及其指向的内容版本。"""

    def __init__(self, rules: list[dict], source_dir: Path) -> None:
        self.rules = rules
        self.source_dir = source_dir
        self.loaded_at = utc_now()
        self._versions = {}
        for rule in rules:
            canonical = json.dumps(rule, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]
            self._versions[rule["rule_id"]] = digest
            rule["_content_hash"] = digest
            rule["_effective_version"] = f"{rule['version']}+{digest[:8]}"

    @property
    def signature(self) -> str:
        """规则集整体签名，用于任务批次记录。"""
        joined = "|".join(sorted(self._versions.values()))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    def version_for(self, rule_id: str) -> str:
        rule = self.get(rule_id)
        return rule["_effective_version"]

    def get(self, rule_id: str) -> dict:
        for rule in self.rules:
            if rule["rule_id"] == rule_id:
                return rule
        raise RuleError(f"规则不存在: {rule_id}")

    def all(self) -> list[dict]:
        return list(self.rules)


def load_rule_file(path: PathLike) -> dict:
    """加载并校验单个 YAML 文件。"""
    rule_path = Path(path)
    if not rule_path.is_file():
        raise RuleError(f"规则文件不存在: {rule_path}")
    try:
        content = yaml.safe_load(rule_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuleError(f"规则文件读取失败 {rule_path.name}: {exc}") from exc
    try:
        rule = validate_rule(content)
    except RuleError as exc:
        raise RuleError(f"{rule_path.name}: {exc}") from exc
    rule["_source_path"] = str(rule_path.resolve())
    return rule


def load_rules(directory: Optional[PathLike] = None) -> RuleSet:
    """加载目录下全部 .yaml/.yml 规则，按 rule_id 去重。"""
    rule_dir = Path(directory or DEFAULT_RULE_DIR).expanduser().resolve()
    if not rule_dir.is_dir():
        raise RuleError(f"规则目录不存在: {rule_dir}")
    files = sorted(rule_dir.glob("*.yaml")) + sorted(rule_dir.glob("*.yml"))
    if not files:
        raise RuleError(f"规则目录中没有 YAML 文件: {rule_dir}")
    rules, seen = [], {}
    for path in files:
        rule = load_rule_file(path)
        if rule["rule_id"] in seen:
            raise RuleError(
                f"规则 ID 重复: {rule['rule_id']} ({seen[rule['rule_id']]})"
            )
        seen[rule["rule_id"]] = path.name
        rules.append(rule)
    return RuleSet(rules, rule_dir)


def rule_version_payload(ruleset: RuleSet) -> list[dict]:
    """生成可持久化的规则版本清单。"""
    return [
        {
            "rule_id": rule["rule_id"],
            "version": rule["_effective_version"],
            "content_hash": rule["_content_hash"],
            "source_path": rule.get("_source_path", ""),
            "loaded_at": ruleset.loaded_at,
        }
        for rule in ruleset.rules
    ]
