"""
原理图专用结构化数据类型

原理图的核心信息不是"段落"，而是：
- 网络（Net）：信号名及其连接的节点
- 电源供应（Power Supply）：电压、来源、负载、去耦电容
- 元件（Component）：位号、参数、封装
- 引脚复用（PinMux）：引脚功能分配
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any


@dataclass
class SchematicPowerSupply:
    """电源供应条目"""
    name: str = ""                    # 如 VDD_NPU_S0
    voltage: str = ""                 # 如 0.75V
    source: str = ""                  # 如 RK806_BUCK2
    max_current: str = ""             # 如 5A
    connected_pins: List[str] = field(default_factory=list)   # 连接的芯片引脚
    decoupling_caps: List[str] = field(default_factory=list)  # 去耦电容列表
    layout_notes: str = ""            # 布局要求，如"电容应放在芯片下方"
    sequence: str = ""                # 上电时序，如 Slot:2


@dataclass
class SchematicNet:
    """网络（信号线）"""
    net_name: str = ""                # 如 VDD_NPU_S0
    nodes: List[str] = field(default_factory=list)  # 连接的节点，如 ["RK3576.1U20", "C1037.1"]


@dataclass
class SchematicComponent:
    """元件"""
    ref: str = ""                     # 位号，如 C1037
    value: str = ""                   # 参数，如 22uF
    package: str = ""                 # 封装，如 C0603
    characteristics: str = ""         # 特性，如 X5R, 6.3V
    connected_nets: List[str] = field(default_factory=list)  # 连接的网络


@dataclass
class SchematicPinMux:
    """引脚复用定义"""
    pin: str = ""                     # 引脚编号，如 1Y24
    ball: str = ""                    # BGA 球编号，如 GPIO0_B6_d
    functions: List[str] = field(default_factory=list)  # 复用功能列表
    default_function: str = ""        # 默认功能


@dataclass
class SchematicPage:
    """原理图单页结构化数据"""
    page_num: int = 0
    page_title: str = ""              # 页面标题，如 "RK3576-Power/GND"
    page_type: str = "unknown"        # power_tree|power_desc|power_layout|pinmux|dcdc|other

    # 各类型页面可能包含的信息
    power_supplies: List[SchematicPowerSupply] = field(default_factory=list)
    nets: List[SchematicNet] = field(default_factory=list)
    components: List[SchematicComponent] = field(default_factory=list)
    pinmux: List[SchematicPinMux] = field(default_factory=list)
    special_notes: List[str] = field(default_factory=list)

    def to_searchable_text(self) -> str:
        """将结构化数据转换为可检索的文本格式
        添加丰富的中文关键词，确保 FTS 能匹配到电源/引脚相关查询
        """
        parts = [f"[原理图第{self.page_num}页 {self.page_title} {self.page_type}]"]

        # 页面级关键词标签（帮助 FTS 召回）
        if self.page_type in ("power_tree", "power_desc", "power_layout", "dcdc"):
            parts.append("电源树 电源描述 电源布局 供电方式 供电网络 PMIC DCDC BUCK LDO 电压 电流")
        if self.page_type == "pinmux":
            parts.append("引脚复用 引脚定义 引脚功能 连接方式 信号名 GPIO PWM UART I2C SPI")

        for ps in self.power_supplies:
            parts.append(f"电源网络 {ps.name} 供电方式 电压{ps.voltage} 来源{ps.source} PMIC通道 最大电流{ps.max_current}")
            if ps.connected_pins:
                parts.append(f"  连接引脚: {', '.join(ps.connected_pins)}")
            if ps.decoupling_caps:
                parts.append(f"  去耦电容: {', '.join(ps.decoupling_caps)}")
            if ps.layout_notes:
                parts.append(f"  布局要求: {ps.layout_notes}")

        # 网络汇总：不展开空节点，节省 token 并便于 FTS 匹配
        if self.nets:
            net_names = [n.net_name for n in self.nets if n.net_name]
            parts.append("网络: " + ", ".join(net_names))

        # 元件汇总：包含位号/参数/封装/附近文本线索
        for comp in self.components:
            comp_parts = [f"元件{comp.ref}"]
            if comp.value:
                comp_parts.append(comp.value)
            if comp.package:
                comp_parts.append(comp.package)
            if comp.characteristics:
                comp_parts.append(comp.characteristics.strip().replace('\n', ' '))
            parts.append(" ".join(comp_parts))

        for pm in self.pinmux:
            parts.append(f"引脚: {pm.pin} 球{pm.ball} 功能: {', '.join(pm.functions)}")

        for note in self.special_notes:
            parts.append(f"备注: {note}")

        return "\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "page_num": self.page_num,
            "page_title": self.page_title,
            "page_type": self.page_type,
            "power_supplies": [
                {
                    "name": ps.name,
                    "voltage": ps.voltage,
                    "source": ps.source,
                    "max_current": ps.max_current,
                    "connected_pins": ps.connected_pins,
                    "decoupling_caps": ps.decoupling_caps,
                    "layout_notes": ps.layout_notes,
                    "sequence": ps.sequence,
                }
                for ps in self.power_supplies
            ],
            "nets": [
                {"net_name": n.net_name, "nodes": n.nodes}
                for n in self.nets
            ],
            "components": [
                {
                    "ref": c.ref,
                    "value": c.value,
                    "package": c.package,
                    "characteristics": c.characteristics,
                    "connected_nets": c.connected_nets,
                }
                for c in self.components
            ],
            "pinmux": [
                {
                    "pin": p.pin,
                    "ball": p.ball,
                    "functions": p.functions,
                    "default_function": p.default_function,
                }
                for p in self.pinmux
            ],
            "special_notes": self.special_notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SchematicPage":
        """从字典反序列化"""
        page = cls(
            page_num=data.get("page_num", 0),
            page_title=data.get("page_title", ""),
            page_type=data.get("page_type", "unknown"),
            special_notes=data.get("special_notes", []),
        )
        for ps_data in data.get("power_supplies", []):
            page.power_supplies.append(SchematicPowerSupply(
                name=ps_data.get("name", ""),
                voltage=ps_data.get("voltage", ""),
                source=ps_data.get("source", ""),
                max_current=ps_data.get("max_current", ""),
                connected_pins=ps_data.get("connected_pins", []),
                decoupling_caps=ps_data.get("decoupling_caps", []),
                layout_notes=ps_data.get("layout_notes", ""),
                sequence=ps_data.get("sequence", ""),
            ))
        for net_data in data.get("nets", []):
            page.nets.append(SchematicNet(
                net_name=net_data.get("net_name", ""),
                nodes=net_data.get("nodes", []),
            ))
        for comp_data in data.get("components", []):
            page.components.append(SchematicComponent(
                ref=comp_data.get("ref", ""),
                value=comp_data.get("value", ""),
                package=comp_data.get("package", ""),
                characteristics=comp_data.get("characteristics", ""),
                connected_nets=comp_data.get("connected_nets", []),
            ))
        for pm_data in data.get("pinmux", []):
            page.pinmux.append(SchematicPinMux(
                pin=pm_data.get("pin", ""),
                ball=pm_data.get("ball", ""),
                functions=pm_data.get("functions", []),
                default_function=pm_data.get("default_function", ""),
            ))
        return page


@dataclass
class SchematicDocument:
    """整份原理图的结构化数据"""
    doc_id: str = ""
    title: str = ""
    pages: List[SchematicPage] = field(default_factory=list)

    def find_power_supply(self, name_keyword: str) -> List[SchematicPowerSupply]:
        """按关键词搜索电源供应"""
        results = []
        kw = name_keyword.lower()
        for page in self.pages:
            for ps in page.power_supplies:
                if kw in ps.name.lower() or kw in ps.voltage.lower() or kw in ps.source.lower():
                    results.append(ps)
        return results

    def find_pinmux(self, function_keyword: str) -> List[SchematicPinMux]:
        """按功能关键词搜索引脚复用"""
        results = []
        kw = function_keyword.lower()
        for page in self.pages:
            for pm in page.pinmux:
                if any(kw in f.lower() for f in pm.functions):
                    results.append(pm)
        return results

    def find_net(self, net_keyword: str) -> List[SchematicNet]:
        """按关键词搜索网络"""
        results = []
        kw = net_keyword.lower()
        for page in self.pages:
            for net in page.nets:
                if kw in net.net_name.lower():
                    results.append(net)
        return results

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "pages": [p.to_dict() for p in self.pages],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SchematicDocument":
        doc = cls(doc_id=data.get("doc_id", ""), title=data.get("title", ""))
        for p_data in data.get("pages", []):
            doc.pages.append(SchematicPage.from_dict(p_data))
        return doc
