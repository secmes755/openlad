"""
原理图专用解析 Prompt 模板

原理图 CAD 图纸的文本提取结果是碎片化的（元件编号、坐标、重复布局元数据）。
直接用正则解析不可靠，使用 LLM 从碎片化文本中提取结构化信息。

设计说明：这里只保留通用提取 prompt。早期按页面类型分派的专用 prompt
（页面分类 / 电源树 / 电源描述 / 电源布局 / PinMux / DCDC）已由
PARSE_GENERIC_SCHEMATIC_PROMPT 这一通用路径取代 —— 专用分支从未被调用，
其 prompt 与解析函数一并移除。新增页面类型的处理应扩展通用提取的字段，
而不是回到按类型分派的写法。
"""

PARSE_GENERIC_SCHEMATIC_PROMPT = """你是一位专业的硬件原理图分析专家。
请从以下原理图页面文本中提取所有可用于后续检索的结构化信息。

该页面来自一份电子原理图（schematic）。页面文本由 CAD 工具提取，可能是碎片化的，
包含元件位号、网络名、引脚名和少量标注。请忽略坐标、页眉页脚、版本号等布局噪声。

页面文本：
{text}

请提取以下信息，输出 JSON：
{
  "page_type": "从以下类型中选择最匹配的一个：power_tree / power_desc / power_layout / pinmux / dcdc / block_diagram / interface / other",
  "components": [
    {
      "ref": "元件位号，如 C1037、R204、U20、L1",
      "value": "元件参数，如 22uF、10k、RK3576",
      "function": "该元件在当前页面的功能描述",
      "connected_nets": ["连接的网络名，如 VDD_NPU_S0"]
    }
  ],
  "nets": [
    {
      "name": "网络名，如 GMAC0_TXD0_M0、PHY0_MDI0_P、VDD_NPU_S0",
      "connected_refs": ["直接连接到该网络的元件位号或引脚名"]
    }
  ],
  "power_supplies": [
    {
      "name": "电源网络名，如 VDD_NPU_S0",
      "voltage": "电压，如 0.75V",
      "source": "来源，如 RK806_BUCK2",
      "max_current": "最大电流",
      "connected_pins": ["连接的芯片引脚"],
      "decoupling_caps": ["去耦电容位号"],
      "layout_notes": "布局/布线要求"
    }
  ],
  "pinmux": [
    {
      "pin": "引脚编号，如 1Y24",
      "ball": "BGA球编号，如 GPIO0_B6_d",
      "functions": ["功能1", "功能2"],
      "default_function": "默认功能"
    }
  ],
  "special_notes": ["任何对理解该页面重要的备注"]
}

注意：
1. 如果某类信息在当前页面不存在，返回空数组
2. 不要编造任何文本中没有出现的信息
3. 保持网络名、位号的原始大小写和拼写
4. 输出必须是合法的 JSON，不要添加解释或 markdown 代码块"""
