"""离线血缘聚合模块（只读）。

按「表单类型 + 字段」聚合多个已落库任务批次，产出沿时间轴的字段血缘视图、
规则版本断点标记与跨版本字段对齐索引。

本模块只通过查询接口读取既有批次数据，不回写数据库，
也不修改任何已完成批次的结果与快照文件。
"""
from archive_extractor.lineage.aggregator import (
    LINEAGE_ARTIFACT_FILES,
    LineageAggregator,
    lineage_run_id,
)

__all__ = ["LineageAggregator", "lineage_run_id", "LINEAGE_ARTIFACT_FILES"]
