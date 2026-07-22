"""
原理图（Schematic）专用解析模块

原理图 CAD 图纸的文本提取结果是碎片化的（元件编号、坐标、重复布局元数据），
无法直接用于向量检索和 LLM 问答。本模块将原理图页面解析为结构化数据：

- 电源供应树（Power Tree）
- 电源描述表格（Power Description）
- 电源布局/布线要求（Power Layout / DCDC Requirements）
- 引脚复用表（PinMux）

结构化数据保存到 doc_pages.schematic_data，检索时优先使用。
"""

from .schematic_types import (
    SchematicPowerSupply,
    SchematicNet,
    SchematicComponent,
    SchematicPinMux,
    SchematicPage,
    SchematicDocument,
)
from .schematic_parser import SchematicParser

__all__ = [
    "SchematicPowerSupply",
    "SchematicNet",
    "SchematicComponent",
    "SchematicPinMux",
    "SchematicPage",
    "SchematicDocument",
    "SchematicParser",
]
