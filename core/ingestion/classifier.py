"""
Universal document classifier
Does not hardcode classification taxonomy; dynamically loaded from industry plugin system
"""
import json
import logging
from typing import Dict, Optional

from ..models import get_model_client
from ..plugins import get_plugin_registry

logger = logging.getLogger(__name__)


class DocumentClassifier:
    """Universal document classifier
    
    Collects classification taxonomy from loaded industry plugins, auto-classifies with LLM.
    If no industry plugins, uses a minimal generic taxonomy.
    """

    def __init__(self):
        self.model_client = get_model_client()

    def _build_taxonomy_prompt(self) -> str:
        """Build taxonomy prompt (with examples to assist LLM understanding)"""
        registry = get_plugin_registry()
        plugins = registry.list_plugins()

        lines = ["Document classification taxonomy:"]
        for pid, info in plugins.items():
            taxonomy = info.get("taxonomy", {})
            if not taxonomy:
                continue
            l1 = taxonomy.get("level1", info["name"])
            desc = taxonomy.get("description", "")
            lines.append(f"\n【{l1}】")
            if desc:
                lines.append(f"  Description: {desc}")
            for l2_item in taxonomy.get("level2", []):
                if isinstance(l2_item, dict):
                    l2_name = l2_item.get("name", "")
                    l3_list = l2_item.get("level3", [])
                    examples = l2_item.get("examples", [])
                    lines.append(f"  Subcategory: {l2_name}")
                    if l3_list:
                        lines.append(f"    Sub-subcategory: {', '.join(l3_list)}")
                    if examples:
                        lines.append(f"    Typical examples: {', '.join(examples[:5])}")
                else:
                    lines.append(f"  Subcategory: {l2_item}")

        if len(lines) == 1:
            lines.append("\n【General Documents】")
            lines.append("  Subcategories: Report, Manual, Contract, Paper, Announcement, Other")
            lines.append("\n【Sub-subcategory】Company/institution name or specific topic (extracted from document content)")

        return "\n".join(lines)

    def classify(self, filename: str, title: str, content_sample: str,
                 plugin=None) -> Dict[str, str]:
        """Three-level document classification
        Directly calls LLM for classification; no content-related hardcoded rules in code
        FIX: Supports passing industry plugin; uses industry plugin classification prompt when manually specified
        V5.0: Chinese system prompt, force product model extraction, no 'Other' fallback.
        """
        # If industry plugin provided, use its classification prompt first
        if plugin and hasattr(plugin, 'ingestion'):
            ingestion_prompts = plugin.ingestion.get_ingestion_prompts()
            classify_system = ingestion_prompts.get("classify_system", "")
            classify_user = ingestion_prompts.get("classify_user", "")
            if classify_system and classify_user:
                logger.info(f"[CLASSIFY] Using industry plugin classification prompt: {plugin.manifest.id if hasattr(plugin, 'manifest') else 'unknown'}")
                prompt = f"""{classify_system}

{classify_user.format(filename=filename, content_sample=content_sample[:3000])}"""
                # Industry plugin prompts typically include JSON format requirements; no additional generic format instructions appended
                try:
                    result = self.model_client.generate_json(prompt, temperature=0.3, max_tokens=1024)
                    if result and isinstance(result, dict):
                        return {
                            "category_level1": result.get("category_level1", "Other"),
                            "category_level2": result.get("category_level2", "Other"),
                            "category_level3": result.get("category_level3", ""),
                            "confidence": result.get("confidence", 0.5)
                        }
                except Exception as e:
                    logger.warning(f"[CLASSIFY] Industry plugin classification failed, falling back to generic classifier: {e}")
                # On failure, continue with generic classifier

        # Generic classifier (auto mode or when no industry plugin specified)
        taxonomy_text = self._build_taxonomy_prompt()
        system_prompt = (
            "你是一个文档分类助手。请根据文档信息提取分类，输出 ONLY JSON。"
            "三级分类必须包含产品型号或公司名，严禁返回 'Other'。"
            "格式: {\"category_level1\": \"...\", \"category_level2\": \"...\", \"category_level3\": \"...\", \"confidence\": 0.0}"
        )
        prompt = f"""{taxonomy_text}

根据以下文档信息确定分类：

文档信息:
- 文件名: {filename}
- 标题: {title}
- 内容样本:
{content_sample[:3000]}

要求:
1. 一级分类: 从上述主分类中选择一个
2. 二级分类: 从选定一级下的子分类中选择一个
3. 三级分类: 必须包含产品型号（如产品型号或公司名称）。"
   从文件名和文本中提取实际出现的名称，严禁返回 'Other'。
4. 置信度: 0.0-1.0

输出 ONLY JSON:
{{"category_level1": "...", "category_level2": "...", "category_level3": "...", "confidence": 0.0}}
"""

        try:
            result = self.model_client.generate_json(prompt, system_prompt=system_prompt, temperature=0.3, max_tokens=1024)
            if result and isinstance(result, dict):
                l1 = result.get("category_level1", "Other")
                l2 = result.get("category_level2", "Other")
                l3 = result.get("category_level3", "Other")
                # LLM may output old category names; dynamic mapping
                if l1 == "Financial & Transaction":
                    l1 = "Financial Reports"
                if l2 in ("Annual Report", "Quarterly Report", "Semi-annual Report", "Audit Report", "Prospectus", "Financial Statements"):
                    l1 = "Financial Reports"
                # Ensure l3 is not "Other" or empty
                if l3 == "Other" or not l3:
                    l3 = self._extract_product_model_from_filename(filename) or "Unknown"
                return {
                    "category_level1": l1,
                    "category_level2": l2,
                    "category_level3": l3,
                    "confidence": result.get("confidence", 0.5)
                }
        except Exception as e:
            logger.error(f"Document classification failed: {e}")

        # Step 2: fallback to generic classification (if applicable)
        taxonomy_text = self._build_taxonomy_prompt()

        prompt = f"""{taxonomy_text}

根据以下文档信息确定分类：

文档信息:
- 文件名: {filename}
- 标题: {title}
- 内容样本:
{content_sample[:3000]}

要求:
1. 一级分类: 从上述主分类中选择一个
2. 二级分类: 从选定一级下的子分类中选择一个
3. 三级分类: 必须包含产品型号（如产品型号或公司名称）。"
   从文件名和文本中提取实际出现的名称，严禁返回 'Other'。
4. 置信度: 0.0-1.0

输出 ONLY JSON:
{{"category_level1": "...", "category_level2": "...", "category_level3": "...", "confidence": 0.0}}
"""

        try:
            result = self.model_client.generate_json(prompt, system_prompt=system_prompt, temperature=0.3, max_tokens=1024)
            if result and isinstance(result, dict):
                l1 = result.get("category_level1", "Other")
                l2 = result.get("category_level2", "Other")
                l3 = result.get("category_level3", "Other")
                if l1 == "Financial & Transaction":
                    l1 = "Financial Reports"
                if l2 in ("Annual Report", "Quarterly Report", "Semi-annual Report", "Audit Report", "Prospectus", "Financial Statements"):
                    l1 = "Financial Reports"
                if l3 == "Other" or not l3:
                    l3 = self._extract_product_model_from_filename(filename) or "Unknown"
                return {
                    "category_level1": l1,
                    "category_level2": l2,
                    "category_level3": l3,
                    "confidence": result.get("confidence", 0.5)
                }
        except Exception as e:
            logger.error(f"Document classification failed: {e}")

        # Fallback: extract from filename
        l3 = self._extract_product_model_from_filename(filename) or "Unknown"
        return {
            "category_level1": "Technical Documentation",
            "category_level2": "Datasheet",
            "category_level3": l3,
            "confidence": 0.5
        }

    def _extract_product_model_from_filename(self, filename: str) -> Optional[str]:
        """Extract product model from filename (e.g. AB1234_Datasheet.pdf -> AB1234)"""
        import re
        # Remove extension
        name = re.sub(r'\.[^.]+$', '', filename)
        # Common patterns: product model codes like AB1234, XY567, etc.
        match = re.search(r'([A-Z][A-Z0-9]{2,})', name)
        if match:
            return match.group(1)
        return None
