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
)

logger = logging.getLogger(__name__)


class SchematicParser:
    """原理图结构化解析器"""

    def __init__(self, model_client=None):
        self.model_client = model_client

    def parse_page(self, page_num: int, page_text: str, page_title: str = "") -> SchematicPage:
        """解析单个原理图页面"""
        # 1. 文本预处理：去噪、去重
        cleaned_text = self._preprocess_text(page_text)

        # 2. 快速规则分类（减少 LLM 调用）
        page_type = self._rule_based_classify(cleaned_text, page_title)

        # 3. 规则分类不明确时，跳过 LLM 分类（避免每页都调用 LLM 导致超时）
        # 规则分类已能捕获 power_tree/power_desc/power_layout/pinmux/dcdc 等关键页面
        # unknown 页面大概率是重复布局/普通接口，用正则轻量提取即可

        page = SchematicPage(
            page_num=page_num,
            page_title=page_title,
            page_type=page_type,
        )

        # 4. 根据页面类型用不同 prompt 提取结构化信息
        # FIX: 只对明确分类为电源/引脚相关的页面调用 LLM 解析，避免每页都调用导致超时
        # 超大页面（>80KB cleaned text）跳过 LLM，只做正则提取（LLM 上下文有限且处理慢）
        cleaned_len = len(cleaned_text)
        skip_llm = cleaned_len > 80000
        if skip_llm:
            logger.debug(f"[SCHEMATIC] 页{page_num} 文本过大({cleaned_len}字符)，跳过 LLM 解析")
            self._regex_extract_power(page, cleaned_text)
            self._regex_extract_pinmux(page, cleaned_text)
        elif page_type in ("power_tree", "power_desc", "dcdc"):
            self._parse_power_page(page, cleaned_text, page_type)
        elif page_type == "power_layout":
            # power_layout 通常只有少量关键要求，正则提取即可
            self._regex_extract_power(page, cleaned_text)
        elif page_type == "pinmux":
            self._parse_pinmux_page(page, cleaned_text)
        elif page_type == "unknown":
            # 规则分类不明确，尝试轻量级正则提取电源网络名（不调用 LLM）
            self._regex_extract_power(page, cleaned_text)
        # other 类型直接跳过，不解析

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
            # 回退到正则
            self._regex_extract_power(page, text)

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
            self._regex_extract_pinmux(page, text)

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

    def _regex_extract_nets(self, page: SchematicPage, text: str):
        """正则提取网络信息"""
        # 简单提取：查找网络名和附近的引脚/元件信息
        # 这是一个轻量级的补充提取
        pass

    def _regex_extract_pinmux(self, page: SchematicPage, text: str):
        """正则提取引脚复用信息（无 LLM 回退）"""
        # 匹配 | Pin | Ball | Func1 | Func2 | ... 格式的表格行
        lines = text.split('\n')
        for line in lines:
            if '|' in line and any(kw in line for kw in ['NPU', 'PWM', 'UART', 'I2C', 'SPI', 'GPIO']):
                parts = [p.strip() for p in line.split('|') if p.strip()]
                if len(parts) >= 3:
                    page.pinmux.append(SchematicPinMux(
                        pin=parts[0],
                        ball=parts[1] if len(parts) > 1 else "",
                        functions=parts[2:],
                    ))

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
