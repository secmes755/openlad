"""
原理图专用解析器

将原理图 PDF 的碎片化文本解析为结构化数据。
策略：
1. 先对页面文本去噪（去重布局元数据、去噪声）
2. 用 LLM 分类页面类型
3. 针对不同类型用不同 prompt 提取结构化信息
"""
import json
import logging
import re
from typing import List, Optional, Dict, Any

from schematic_types import SchematicPage, SchematicPowerSupply, SchematicNet, SchematicComponent, SchematicPinMux
from schematic_prompts import (
    PAGE_TYPE_CLASSIFICATION_PROMPT,
    PARSE_POWER_TREE_PROMPT,
    PARSE_POWER_DESC_PROMPT,
    PARSE_POWER_LAYOUT_PROMPT,
    PARSE_PINMUX_PROMPT,
    PARSE_DCDC_PROMPT,
    PARSE_GENERIC_SCHEMATIC_PROMPT,
)

logger = logging.getLogger(__name__)


class SchematicParser:
    """原理图结构化解析器"""

    def __init__(self, model_client=None):
        self.model_client = model_client

    def parse_page(self, page_num: int, page_text: str, page_title: str = "",
                   page_image: Any = None) -> SchematicPage:
        """解析单个原理图页面"""
        # 1. 文本预处理：去噪、去重
        cleaned_text = self._preprocess_text(page_text)

        # 2. 快速规则分类（减少 LLM 调用）
        page_type = self._rule_based_classify(cleaned_text, page_title)

        page = SchematicPage(
            page_num=page_num,
            page_title=page_title,
            page_type=page_type,
        )

        # 3. 提取结构化信息：
        #    - 对所有页面先用规则提取元件/网络（快、稳定）
        #    - 对引脚复用/电源描述等表格密集型页面，再叠加轻量级 LLM 理解
        cleaned_len = len(cleaned_text)
        self._extract_components_and_nets(page, cleaned_text)
        self._regex_extract_power(page, cleaned_text)

        # LLM 仅在信息密度高且规则难以处理的页面使用，避免普通原理图页大量调用 VLM
        use_llm = (
            self.model_client is not None
            and 50 <= cleaned_len <= 40000
            and page.page_type in ("pinmux", "power_desc", "power_tree", "dcdc", "unknown")
        )
        if use_llm:
            self._parse_generic_schematic_page(page, cleaned_text)

        return page

    def _preprocess_text(self, text: str) -> str:
        """原理图文本预处理：去噪、去重、控制长度"""
        if not text:
            return ""

        lines = text.split('\n')
        cleaned_lines = []
        seen_lines = set()

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 去重：完全相同的行（原理图中大量重复的布局元数据）
            if line in seen_lines:
                continue
            seen_lines.add(line)

            # 过滤纯噪声行
            if self._is_noise_line(line):
                continue

            cleaned_lines.append(line)

        # 控制长度：原理图页面可能非常大（如 230KB），LLM 上下文有限
        # 保留前 8000 字符 + 后 4000 字符（中间通常是重复的电容阵列）
        result = '\n'.join(cleaned_lines)
        if len(result) > 12000:
            # 保留头部（通常是标题和主要网络）和尾部（通常是备注和要求）
            head = '\n'.join(cleaned_lines[:300])
            tail = '\n'.join(cleaned_lines[-150:])
            result = f"{head}\n\n...[中间省略 {len(cleaned_lines) - 450} 行]...\n\n{tail}"

        return result

    def _is_noise_line(self, line: str) -> bool:
        """判断是否为噪声行（布局元数据、纯坐标等）"""
        noise_patterns = [
            r'^(Page Title|Page Size|Document Title|Date:|Sheet|of|PCB NO\.|Sch Designer|Pcb Designer|Project Stage|Page \d+)$',
            r'^[A-Z]3$',  # 页面尺寸 A3
            r'^\d+$',  # 纯数字（坐标）
            r'^[A-Z]\d+$',  # 坐标如 A1, B2
            r'^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),.*$',  # 日期
            r'^K7_V\d+\.\d+$',  # 版本号
            r'^[A-Z]{2,4}$',  # 纯大写缩写如 TIM, DR4
        ]
        for p in noise_patterns:
            if re.match(p, line, re.IGNORECASE):
                return True
        return False

    def _rule_based_classify(self, text: str, page_title: str) -> str:
        """基于规则的快速页面分类"""
        text_lower = text.lower()
        title_lower = page_title.lower()

        # 页面标题判断
        if "power" in title_lower and "gnd" in title_lower:
            return "power_layout"
        if "power/gnd" in title_lower:
            return "power_layout"

        # 文本内容判断
        if "power tree" in text_lower or "power on sequence" in text_lower:
            return "power_tree"
        if "power description" in text_lower or "work voltage" in text_lower:
            return "power_desc"
        if "caps should be placed" in text_lower or "placed under" in text_lower:
            return "power_layout"
        if "pin" in text_lower and any(kw in text_lower for kw in ['mux', 'function', 'gpio', 'pwm', 'uart', 'i2c']):
            # 进一步确认是 pinmux 而不是普通引脚列表
            if text.count('PWM') > 3 or text.count('UART') > 3 or text.count('GPIO') > 5:
                return "pinmux"
        if "dcdc" in text_lower or "buck" in text_lower and "w≥" in text_lower:
            return "dcdc"

        return "unknown"

    def _llm_classify(self, text: str) -> str:
        """用 LLM 分类页面类型"""
        if not self.model_client:
            return "unknown"

        sample = text[:2000] if len(text) > 2000 else text
        prompt = PAGE_TYPE_CLASSIFICATION_PROMPT.replace("{text_sample}", sample)

        try:
            result = self.model_client.generate(prompt, temperature=0.1, max_tokens=50)
            result = result.strip().lower()
            valid_types = {"power_tree", "power_desc", "power_layout", "pinmux", "dcdc", "other"}
            if result in valid_types:
                return result
            # 模糊匹配
            if "power" in result and "tree" in result:
                return "power_tree"
            if "power" in result and "desc" in result:
                return "power_desc"
            if "power" in result and "layout" in result:
                return "power_layout"
            if "pin" in result:
                return "pinmux"
            if "dcdc" in result:
                return "dcdc"
        except Exception as e:
            logger.warning(f"[SCHEMATIC] LLM 分类失败: {e}")

        return "other"

    def _parse_power_page(self, page: SchematicPage, text: str, page_type: str):
        """解析电源相关页面"""
        if not self.model_client:
            # 无 LLM 时回退到正则提取
            self._regex_extract_power(page, text)
            return

        # 选择 prompt（使用 replace 而非 format，避免 JSON 示例中的 {} 被误解析）
        if page_type == "power_tree":
            prompt = PARSE_POWER_TREE_PROMPT.replace("{text}", text)
        elif page_type == "power_desc":
            prompt = PARSE_POWER_DESC_PROMPT.replace("{text}", text)
        elif page_type == "power_layout":
            prompt = PARSE_POWER_LAYOUT_PROMPT.replace("{text}", text)
        elif page_type == "dcdc":
            prompt = PARSE_DCDC_PROMPT.replace("{text}", text)
        else:
            prompt = PARSE_POWER_TREE_PROMPT.replace("{text}", text)

        try:
            result = self.model_client.generate(prompt, temperature=0.1, max_tokens=4096)
            data = self._extract_json(result)

            if isinstance(data, list):
                # power_tree / power_desc 返回数组
                for item in data:
                    page.power_supplies.append(SchematicPowerSupply(
                        name=item.get("name", ""),
                        voltage=item.get("voltage", ""),
                        source=item.get("source", ""),
                        max_current=item.get("max_current", ""),
                        connected_pins=item.get("connected_pins", []),
                        decoupling_caps=item.get("decoupling_caps", []),
                        layout_notes=item.get("layout_notes", item.get("notes", "")),
                        sequence=item.get("sequence", ""),
                    ))
            elif isinstance(data, dict):
                # power_layout / dcdc 返回对象
                for item in data.get("power_supplies", []):
                    page.power_supplies.append(SchematicPowerSupply(
                        name=item.get("name", ""),
                        voltage=item.get("voltage", ""),
                        source=item.get("source", ""),
                        max_current=item.get("max_current", ""),
                        connected_pins=item.get("connected_pins", []),
                        decoupling_caps=item.get("decoupling_caps", []),
                        layout_notes=item.get("layout_notes", item.get("notes", "")),
                        sequence=item.get("sequence", ""),
                    ))
                for item in data.get("components", []):
                    page.components.append(SchematicComponent(
                        ref=item.get("ref", ""),
                        value=item.get("value", ""),
                        package=item.get("package", ""),
                        characteristics=item.get("characteristics", ""),
                    ))
                page.special_notes.extend(data.get("special_notes", []))

            # 同时提取网络信息
            self._regex_extract_nets(page, text)

        except Exception as e:
            logger.warning(f"[SCHEMATIC] LLM 解析电源页面失败 (页{page.page_num}): {e}")
            # 回退到规则提取
            self._extract_components_and_nets(page, text)

    def _parse_pinmux_page(self, page: SchematicPage, text: str):
        """解析引脚复用页面"""
        if not self.model_client:
            self._regex_extract_pinmux(page, text)
            return

        prompt = PARSE_PINMUX_PROMPT.replace("{text}", text)
        try:
            result = self.model_client.generate(prompt, temperature=0.1, max_tokens=4096)
            data = self._extract_json(result)
            if isinstance(data, list):
                for item in data:
                    page.pinmux.append(SchematicPinMux(
                        pin=item.get("pin", ""),
                        ball=item.get("ball", ""),
                        functions=item.get("functions", []),
                        default_function=item.get("default_function", ""),
                    ))
        except Exception as e:
            logger.warning(f"[SCHEMATIC] LLM 解析 PinMux 页面失败 (页{page.page_num}): {e}")
            self._extract_components_and_nets(page, text)

    def _regex_extract_power(self, page: SchematicPage, text: str):
        """正则提取电源信息（无 LLM 回退）"""
        # 匹配 VDD_xxx_xxx 或 VCC_xxx_xxx 格式的电源网络
        power_patterns = [
            r'(VDD_[A-Z0-9_]+)\s+(\d+\.?\d*V)',
            r'(VCC_[A-Z0-9_]+)\s+(\d+\.?\d*V)',
            r'(VDDA_[A-Z0-9_]+)\s+(\d+\.?\d*V)',
        ]
        seen = set()
        for pattern in power_patterns:
            for m in re.finditer(pattern, text):
                name = m.group(1)
                voltage = m.group(2)
                if name not in seen:
                    seen.add(name)
                    page.power_supplies.append(SchematicPowerSupply(
                        name=name,
                        voltage=voltage,
                    ))

    def _extract_components_and_nets(self, page: SchematicPage, text: str):
        """规则提取元件位号与网络名，并补全元件附近的参数/连接信息。

        原理图 CAD 提取出的文本顺序通常混乱，无法精确重建 Netlist，但可以：
        1. 用正则提取所有标准位号（C1037 / R204 / U20 等）；
        2. 对每个位号，截取后面一小段文本作为 value/package 线索；
        3. 用通用模式提取网络名（含下划线的总线/电源/信号名），作为可检索关键词。
        """
        # 常见封装尺寸、材质标记，不是元件位号
        package_sizes = {"0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512"}
        dielectrics = {"X5R", "X7R", "X6S", "X7S", "NPO", "Y5V", "COG", "NP0"}
        # 合并 LLM 已提取的位号，避免重复
        seen_refs = {c.ref for c in page.components if c.ref}

        # 辅助：在位号后截取“局部片段”（到下一个位号或换行为止）
        ref_pattern = re.compile(r'\b([CRUQLDBVFMJXT]\d+[A-Z]?)\b')

        def _local_tail(pos: int) -> str:
            """截取当前位号后、到下一个位号或换行之前的文本。"""
            window = text[pos:pos+120]
            # 优先在下一个位号前截断
            nxt = ref_pattern.search(window)
            if nxt and nxt.start() > 0:
                window = window[:nxt.start()]
            # 再按换行截断，取第一行
            window = window.split('\n')[0]
            return window.strip()

        def _extract_value(tokens: List[str], prefix: str) -> str:
            """从局部 token 列表中按元件类型提取 value。"""
            # 电容：必须含 F；电阻：必须含 Ω 或 K/M/R 后缀；电感：必须含 H
            for tok in tokens[:8]:
                t = tok.strip()
                if not t or len(t) > 20 or '_' in t:
                    continue
                if prefix == "C":
                    m = re.match(r'([\d.]+\s*[uUnNpPmMkKfF]?F)$', t, re.IGNORECASE)
                    if m:
                        return m.group(1)
                elif prefix == "R":
                    m = re.match(r'([\d.]+\s*(?:[KkMmRr]Ω?|Ω|ohm|ohms)?)$', t, re.IGNORECASE)
                    if m and re.search(r'[KkMmRrΩΩ]|ohm', t, re.IGNORECASE):
                        return m.group(1)
                    # 纯阻值带 0R/10R 写法
                    m = re.match(r'(\d+[Rr]\d*)$', t)
                    if m:
                        return m.group(1)
                elif prefix == "L":
                    m = re.match(r'([\d.]+\s*[uUnNmM]?H)$', t, re.IGNORECASE)
                    if m:
                        return m.group(1)
            return ""

        def _extract_package(tokens: List[str]) -> str:
            """从局部 token 列表中提取封装尺寸。"""
            for tok in tokens[:8]:
                t = tok.strip()
                m = re.match(r'([CR]\d{4})$', t, re.IGNORECASE) or re.match(r'(0201|0402|0603|0805|1206|1210)$', t)
                if m:
                    return m.group(1)
            return ""

        for m in ref_pattern.finditer(text):
            ref = m.group(1)
            if ref in seen_refs:
                continue
            seen_refs.add(ref)

            # 基本过滤
            if len(ref) > 10 or len(ref) < 2:
                continue
            prefix = ref[0]
            num_part = re.sub(r'[A-Z]$', '', ref[1:], count=1, flags=re.IGNORECASE)
            # 材质/封装标记不是元件位号
            if ref.upper() in dielectrics or num_part in package_sizes:
                continue
            if num_part in package_sizes and prefix in {"R", "C", "L"}:
                continue
            if re.fullmatch(r'0+', num_part):
                continue

            tail = _local_tail(m.end())
            tokens = tail.split()
            value = _extract_value(tokens, prefix)
            package = _extract_package(tokens)

            page.components.append(SchematicComponent(
                ref=ref,
                value=value,
                package=package,
                characteristics=tail,
                connected_nets=[],
            ))

        # 2) 提取网络名：
        #    a) 含下划线且大写字母开头的总线/信号名，如 MIPI_DPHY_CSI0_D0P、GMAC0_TXD0_M0
        #    b) 电源/地网络，如 VCC_1V8、VDD_NPU_S0、GND
        #    c) 常见接口前缀的单一名称，如 HDMI_TX_CEC_M1、USB3_OTG0
        net_patterns = [
            # 总线/信号：大写开头，含下划线和数字
            r'\b([A-Z][A-Z0-9]{1,}(?:_[A-Z0-9]+)+)\b',
            # 电源网络
            r'\b((?:VCC|VDD|VSS|VPP|VEE|AVDD|DVDD|RVDD|MVDD|NVDD|PVDD|UVDD|IOVDD|BUCK|LDO)\w*)\b',
            # 地网络
            r'\b(GND\w*)\b',
            # 通用接口前缀
            r'\b((?:MIPI|USB|HDMI|DP|GMAC|PCIE|SATA|SDMMC|EMMC|DDR|LPDDR|UART|I2C|SPI|PWM|GPIO|CSI|DSI|NPOR|RESET|XOUT|CLK|PMIC|TSADC|SAI|PDM|SPDIF|JTAG|CAM)[A-Z0-9_]*)\b',
        ]
        seen_nets = {n.net_name for n in page.nets if n.net_name}
        for pat in net_patterns:
            for m in re.finditer(pat, text):
                name = m.group(1).rstrip('_')
                if len(name) < 3 or name in seen_nets:
                    continue
                # 过滤掉与位号重合的
                if name in seen_refs:
                    continue
                # 过滤明显噪声：纯数字、日期、版本号
                if re.fullmatch(r'\d+', name) or re.match(r'20\d{2}|V\d+\.\d+', name):
                    continue
                # 过滤 coordinate-like A1 / B2
                if re.fullmatch(r'[A-Z]\d+', name):
                    continue
                seen_nets.add(name)
                page.nets.append(SchematicNet(net_name=name, nodes=[]))

        # 3) 把 page_type 从 unknown 改成更具体的类型（仅基于文本特征）
        text_lower = text.lower()
        if page.page_type == "unknown":
            if any(kw in text_lower for kw in ["power description", "power sequence", "work voltage", "sleep current"]):
                page.page_type = "power_desc"
            elif "caps should be placed" in text_lower or "placed under" in text_lower:
                page.page_type = "power_layout"
            elif ("pin" in text_lower or "ball" in text_lower) and text.count('GPIO') + text.count('PWM') + text.count('UART') + text.count('I2C') > 10:
                page.page_type = "pinmux"

    def _parse_generic_schematic_page(self, page: SchematicPage, text: str):
        """通用原理图页面解析：提取元件、网络、电源、引脚复用等结构化信息"""
        if not self.model_client:
            self._extract_components_and_nets(page, text)
            return

        prompt = PARSE_GENERIC_SCHEMATIC_PROMPT.replace("{text}", text)
        try:
            result = self.model_client.generate(prompt, temperature=0.1, max_tokens=4096)
            data = self._extract_json(result)

            if isinstance(data, dict):
                # 页面类型：如果 LLM 给出了更具体的类型，采用；否则保留规则分类
                inferred_type = data.get("page_type", "").strip().lower()
                if inferred_type and inferred_type != page.page_type:
                    page.page_type = inferred_type

                for item in data.get("components", []):
                    page.components.append(SchematicComponent(
                        ref=item.get("ref", ""),
                        value=item.get("value", ""),
                        package=item.get("package", ""),
                        characteristics=item.get("function", "") or item.get("characteristics", ""),
                        connected_nets=item.get("connected_nets", []),
                    ))

                for item in data.get("nets", []):
                    page.nets.append(SchematicNet(
                        net_name=item.get("name", ""),
                        nodes=item.get("connected_refs", []),
                    ))

                for item in data.get("power_supplies", []):
                    page.power_supplies.append(SchematicPowerSupply(
                        name=item.get("name", ""),
                        voltage=item.get("voltage", ""),
                        source=item.get("source", ""),
                        max_current=item.get("max_current", ""),
                        connected_pins=item.get("connected_pins", []),
                        decoupling_caps=item.get("decoupling_caps", []),
                        layout_notes=item.get("layout_notes", item.get("notes", "")),
                        sequence=item.get("sequence", ""),
                    ))

                for item in data.get("pinmux", []):
                    page.pinmux.append(SchematicPinMux(
                        pin=item.get("pin", ""),
                        ball=item.get("ball", ""),
                        functions=item.get("functions", []),
                        default_function=item.get("default_function", ""),
                    ))

                page.special_notes.extend(data.get("special_notes", []))

        except Exception as e:
            logger.warning(f"[SCHEMATIC] 通用解析失败 (页{page.page_num}): {e}")
            self._extract_components_and_nets(page, text)

    def _extract_json(self, text: str) -> Any:
        """从 LLM 输出中提取 JSON"""
        # 尝试直接解析
        text = text.strip()
        if text.startswith('```json'):
            text = text[7:]
        if text.startswith('```'):
            text = text[3:]
        if text.endswith('```'):
            text = text[:-3]
        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试提取 JSON 数组/对象
            arr_match = re.search(r'\[[\s\S]*\]', text)
            if arr_match:
                try:
                    return json.loads(arr_match.group())
                except json.JSONDecodeError:
                    pass
            obj_match = re.search(r'\{[\s\S]*\}', text)
            if obj_match:
                try:
                    return json.loads(obj_match.group())
                except json.JSONDecodeError:
                    pass
            raise ValueError(f"无法从 LLM 输出中提取 JSON: {text[:200]}")
