"""
原理图专用解析 Prompt 模板

原理图 CAD 图纸的文本提取结果是碎片化的（元件编号、坐标、重复布局元数据）。
直接用正则解析不可靠，使用 LLM 从碎片化文本中提取结构化信息。
"""

PAGE_TYPE_CLASSIFICATION_PROMPT = """你是一位专业的硬件原理图分析专家。
请分析以下原理图页面的提取文本，判断页面类型。

页面文本：
{text_sample}

请从以下类型中选择最匹配的一个：
- power_tree: 电源树/电源框图，显示各电源的来源和分配关系（如 VCC12V → DC/DC → VDD_NPU_S0）
- power_desc: 电源描述表格，列出各电源的电压、电流、时序、PMIC通道等参数
- power_layout: 电源布局图，显示电容/电感放置位置、布线要求
- pinmux: 引脚复用表，显示芯片各引脚的功能分配
- dcdc: DCDC/PMIC 设计要求和布线规范
- other: 其他类型（如封面、目录、通用说明）

只输出类型名称，不要解释。"""


PARSE_POWER_TREE_PROMPT = """你是一位专业的硬件原理图分析专家。
请从以下原理图"电源树"页面文本中提取电源供应信息。

电源树页面显示的是从输入电源（如 12V 适配器）到各负载的分配关系。
通常包含：DC/DC 转换器、PMIC 通道（BUCK1~BUCK10、PLDO1~PLDO6、NLDO1~NLDO5）、
输出电压、电流限制、以及供电目标（如 VDD_NPU_S0、VDD_CPU_BIG_S0）。

页面文本：
{text}

请提取所有电源供应条目，输出 JSON 数组：
[
  {
    "name": "电源网络名，如 VDD_NPU_S0",
    "voltage": "电压值，如 0.75V",
    "source": "来源，如 RK806_BUCK2 或 DC/DC",
    "max_current": "最大电流，如 5A",
    "connected_pins": ["连接的芯片引脚编号"],
    "decoupling_caps": ["去耦电容，如 C1037_22uF"],
    "sequence": "上电时序，如 Slot:2",
    "layout_notes": "布局备注"
  }
]

注意：
1. 只提取与电源供应相关的条目，不要提取无关的接口信号
2. 如果某条信息在文本中未出现，留空字符串
3. 保持原文中的网络名称（如 VDD_NPU_S0 不要简化为 NPU）
4. 输出必须是合法的 JSON 数组，不要添加解释"""


PARSE_POWER_DESC_PROMPT = """你是一位专业的硬件原理图分析专家。
请从以下原理图"电源描述"页面文本中提取电源参数表格信息。

电源描述页面通常是一个表格，列出各电源的：
- Power Name（电源名）
- PMIC Channel（PMIC通道，如 RK806_BUCK1）
- Supply Limit（电流限制）
- Work Voltage（工作电压）
- Default Voltage（默认电压）
- Power ON/OFF Sequence（上电时序）
- Peak Current（峰值电流）
- Sleep Current（休眠电流）

页面文本：
{text}

请提取所有电源参数，输出 JSON 数组：
[
  {
    "name": "电源网络名，如 VDD_NPU_S0",
    "voltage": "电压值，如 0.75V",
    "source": "PMIC通道，如 RK806_BUCK2",
    "max_current": "最大电流/限制，如 5A",
    "sequence": "上电时序 Slot",
    "notes": "其他备注"
  }
]

注意：
1. 如果文本中电源名和 PMIC 通道在不同行，请将它们正确配对
2. 输出必须是合法的 JSON 数组"""


PARSE_POWER_LAYOUT_PROMPT = """你是一位专业的硬件原理图分析专家。
请从以下原理图"电源布局"页面文本中提取布局和布线要求。

电源布局页面显示的是：
- 芯片电源球的分布
- 去耦电容的放置位置（如 "Caps should be placed under the U1000 package"）
- 布线要求（如 "distance must be less than 15mm"）
- 电感和电容的规格

页面文本：
{text}

请提取以下信息，输出 JSON：
{
  "power_supplies": [
    {
      "name": "电源网络名",
      "voltage": "电压",
      "connected_pins": ["引脚编号"],
      "decoupling_caps": ["电容位号和参数"],
      "layout_notes": "布局要求"
    }
  ],
  "components": [
    {
      "ref": "位号",
      "value": "参数",
      "package": "封装",
      "characteristics": "特性"
    }
  ],
  "special_notes": ["所有布局/布线备注"]
}

注意：
1. 特别关注电容放置要求（如"放在芯片下方""距离小于15mm"）
2. 提取所有与电源相关的元件
3. 输出必须是合法的 JSON"""


PARSE_PINMUX_PROMPT = """你是一位专业的硬件原理图分析专家。
请从以下原理图"引脚复用"页面文本中提取引脚功能分配信息。

引脚复用页面通常是一个表格，列出芯片各引脚的可选功能，如：
Pin | Ball | Function1 | Function2 | Function3 | ...

页面文本：
{text}

请提取所有引脚复用条目，输出 JSON 数组：
[
  {
    "pin": "引脚编号，如 1Y24",
    "ball": "BGA球编号，如 GPIO0_B6_d",
    "functions": ["功能1", "功能2", "功能3"],
    "default_function": "默认功能"
  }
]

注意：
1. 只提取包含功能分配的条目，不要提取纯电源/地引脚
2. 如果某行包含 NPU 相关功能（如 NPU_AVS），确保保留
3. 输出必须是合法的 JSON 数组"""


PARSE_DCDC_PROMPT = """你是一位专业的硬件原理图分析专家。
请从以下原理图"DCDC/PMIC 设计要求"页面文本中提取设计规范。

DCDC 页面通常包含：
- DCDC 芯片型号和拓扑
- 输入/输出电压和电流
- 电感、电容选型
- PCB 布线要求（如 "W≥6mm"、"placed under the NPU Ball"）
- 布局约束（如 "distance must be less than 15mm"）

页面文本：
{text}

请提取以下信息，输出 JSON：
{
  "power_supplies": [
    {
      "name": "电源网络名",
      "voltage": "输出电压",
      "source": "DCDC芯片/拓扑",
      "max_current": "最大电流",
      "layout_notes": "布局和布线要求"
    }
  ],
  "components": [
    {
      "ref": "位号",
      "value": "参数",
      "package": "封装",
      "characteristics": "特性"
    }
  ],
  "special_notes": ["所有设计规范备注"]
}

注意：
1. 特别关注布线宽度、距离、放置位置等要求
2. 输出必须是合法的 JSON"""
