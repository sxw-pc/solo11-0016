"""归档台账生成与多格式导出。"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from archive_extractor.utils.common import utc_now
from archive_extractor.utils.hash import file_sha256

META_COLUMNS = [
    ("result_id", "结果ID"),
    ("task_id", "任务ID"),
    ("form_type", "表单类型"),
    ("status", "结果状态"),
    ("error_level", "最高错误等级"),
    ("source_rel_path", "原始文件"),
    ("source_md5", "文件MD5"),
    ("rule_version", "规则版本"),
    ("version", "结果版本"),
    ("created_at", "提取时间"),
]


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _sha256(path: Path) -> str:
    return file_sha256(path)


class Archiver:
    """按任务筛选结果并生成可校验的档案台账。"""

    def __init__(self, db, file_store) -> None:
        self.db = db
        self.store = file_store

    def _load_records(self, task_id: int, filters: Optional[dict] = None) -> list[dict]:
        criteria = dict(filters or {})
        criteria["task_id"] = task_id
        rows = self.db.list_results(
            criteria, page=1, page_size=100000, include_deleted=False
        )
        for row in rows:
            row["data"] = json.loads(row["data_json"])
        return [row for row in rows if row["status"] in {"valid", "warning"}]

    @staticmethod
    def _columns(records: list[dict]) -> list[str]:
        keys: list[str] = []
        for record in records:
            for key in record["data"]:
                if key not in keys:
                    keys.append(key)
        return keys

    def _errors(self, task_id: int) -> list[dict]:
        return self.db.query(
            """SELECT v.*, r.source_rel_path, r.rule_version FROM validations v
               JOIN results r ON r.id = v.result_id WHERE r.task_id = ?
               AND r.is_deleted = 0 ORDER BY v.id""",
            (task_id,),
        )

    def _traces(self, task_id: int) -> list[dict]:
        return self.db.query(
            """SELECT t.*, r.source_rel_path, r.form_type FROM traces t
               JOIN results r ON r.id = t.result_id WHERE r.task_id = ?
               AND r.is_deleted = 0 ORDER BY t.id""",
            (task_id,),
        )

    def _stats(self, task_id: int, records: list[dict]) -> dict:
        task = self.db.get_task(task_id) or {}
        levels = {"error": 0, "warning": 0, "info": 0}
        for row in self._errors(task_id):
            levels[row["level"]] = levels.get(row["level"], 0) + 1
        return {
            "task_id": task_id,
            "task_name": task.get("name", ""),
            "task_status": task.get("status", ""),
            "source_dir": task.get("root_dir", ""),
            "total_results": len(records),
            "valid": sum(row["status"] == "valid" for row in records),
            "warning": sum(row["status"] == "warning" for row in records),
            "invalid": sum(row["status"] == "invalid" for row in records),
            "validation_error": levels["error"],
            "validation_warning": levels["warning"],
            "validation_info": levels["info"],
            "exported_at": utc_now(),
        }

    def export(
        self,
        task_id: int,
        fmt: str = "xlsx",
        filters: Optional[dict] = None,
        options: Optional[dict] = None,
    ) -> dict:
        """导出 CSV/JSON/XLSX，并生成 SHA256 校验值。"""
        fmt = fmt.lower()
        if fmt not in {"csv", "json", "xlsx"}:
            raise ValueError(f"不支持的导出格式: {fmt}")
        options = {
            "include_trace": True,
            "include_errors": True,
            "include_history": False,
        } | (options or {})
        records = self._load_records(task_id, filters)
        errors = self._errors(task_id) if options["include_errors"] else []
        traces = self._traces(task_id) if options["include_trace"] else []
        stats = self._stats(task_id, records)
        path = self.store.new_export_path(fmt, f"task_{task_id}")
        if fmt == "csv":
            self._write_csv(path, records)
        elif fmt == "json":
            self._write_json(path, stats, records, errors, traces)
        else:
            self._write_xlsx(path, stats, records, errors, traces)
        digest = _sha256(path)
        sidecar = path.with_suffix(path.suffix + ".sha256")
        sidecar.write_text(f"{digest}  {path.name}\n", encoding="ascii")
        self.db.add_export(task_id, fmt, str(path), digest)
        return {
            "format": fmt, "path": str(path), "sha256": digest,
            "sidecar": str(sidecar), "records": len(records), "stats": stats,
        }

    def _write_csv(self, path: Path, records: list[dict]) -> None:
        fields = self._columns(records)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([label for _, label in META_COLUMNS] + fields)
            for record in records:
                row = [_cell(record.get(key, "")) for key, _ in META_COLUMNS]
                row.extend(_cell(record["data"].get(key, "")) for key in fields)
                writer.writerow(row)

    def _write_json(self, path, stats, records, errors, traces) -> None:
        payload = {
            "batch": stats,
            "records": [
                {**{key: row.get(key) for key, _ in META_COLUMNS},
                 "data": row["data"]} for row in records
            ],
            "errors": errors,
            "traces": traces,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _write_xlsx(self, path, stats, records, errors, traces) -> None:
        workbook = Workbook()
        workbook.remove(workbook.active)
        header_font = Font(bold=True, color="FFFFFF")
        fill = PatternFill("solid", fgColor="3B5B7A")
        self._sheet_kv(workbook, "批次信息", stats, header_font, fill)
        self._sheet_stats(workbook, stats, header_font, fill)
        fields = self._columns(records)
        sheet = workbook.create_sheet("结果明细")
        headers = [label for _, label in META_COLUMNS] + fields
        self._append_header(sheet, headers, header_font, fill)
        for record in records:
            values = [_cell(record.get(key, "")) for key, _ in META_COLUMNS]
            values.extend(_cell(record["data"].get(key, "")) for key in fields)
            sheet.append(values)
        self._auto_width(sheet)
        self._sheet_errors(workbook, errors, header_font, fill)
        self._sheet_traces(workbook, traces, header_font, fill)
        workbook.save(path)

    @staticmethod
    def _append_header(sheet, headers, font, fill) -> None:
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = font
            cell.fill = fill

    @staticmethod
    def _auto_width(sheet) -> None:
        for index, column in enumerate(sheet.columns, 1):
            width = min(50, max(10, max(len(str(cell.value or "")) for cell in column)))
            sheet.column_dimensions[get_column_letter(index)].width = width

    def _sheet_kv(self, workbook, title, stats, font, fill) -> None:
        sheet = workbook.create_sheet(title)
        self._append_header(sheet, ["项目", "值"], font, fill)
        for key, value in stats.items():
            sheet.append([key, _cell(value)])
        self._auto_width(sheet)

    def _sheet_stats(self, workbook, stats, font, fill) -> None:
        sheet = workbook.create_sheet("提取统计")
        self._append_header(sheet, ["统计项", "数量"], font, fill)
        keys = ["total_results", "valid", "warning", "invalid",
                "validation_error", "validation_warning", "validation_info"]
        for key in keys:
            sheet.append([key, stats.get(key, 0)])
        self._auto_width(sheet)

    def _sheet_errors(self, workbook, errors, font, fill) -> None:
        sheet = workbook.create_sheet("错误记录")
        self._append_header(
            sheet, ["结果ID", "文件", "字段", "等级", "原因", "校验器"], font, fill
        )
        for row in errors:
            sheet.append([
                row["result_id"], row["source_rel_path"], row["field"],
                row["level"], row["message"], row["validator"],
            ])
        if not errors:
            sheet.append(["", "", "", "", "无错误记录或未启用错误导出", ""])
        self._auto_width(sheet)

    def _sheet_traces(self, workbook, traces, font, fill) -> None:
        sheet = workbook.create_sheet("溯源索引")
        self._append_header(
            sheet,
            ["结果ID", "文件", "字段", "选择器", "方式", "行号", "规则版本",
             "原始值", "是否派生", "上游字段", "表达式", "联动状态", "降级原因"],
            font,
            fill,
        )
        for row in traces:
            sheet.append([
                row["result_id"], row["source_rel_path"], row["field"],
                row["selector"], row["selector_type"], row["source_line"],
                row["rule_version"], row["raw_value"],
                "是" if row.get("derived") else "",
                row.get("upstream_fields", ""), row.get("expression", ""),
                row.get("linkage_status", ""), row.get("linkage_reason", ""),
            ])
        if not traces:
            sheet.append(["", "", "", "", "", "", "", "未启用溯源导出",
                          "", "", "", "", ""])
        self._auto_width(sheet)

    def export_error_list(self, task_id: int, output: Path | str) -> Path:
        """单独导出错误清单 CSV。"""
        target = Path(output)
        errors = self._errors(task_id)
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["结果ID", "文件", "字段", "等级", "原因", "校验器"])
            for row in errors:
                writer.writerow([
                    row["result_id"], row["source_rel_path"], row["field"],
                    row["level"], row["message"], row["validator"],
                ])
        return target

    @staticmethod
    def verify_export(path: Path | str, expected: str) -> bool:
        """验证导出文件 SHA256，防止归档后被篡改。"""
        return hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected
