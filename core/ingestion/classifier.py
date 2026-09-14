"""
Universal document classifier
Does not hardcode classification taxonomy; dynamically loaded from industry plugin system
"""
import logging

from ..models import get_model_client
from ..plugins import get_plugin_registry

logger = logging.getLogger(__name__)

# Values that mean "this document could not be classified". They are normalized
# to None (SQL NULL) and never stored as a label: downstream consumers ROUTE on
# these fields (`category_level1 or industry_package_id or "general"` in the
# retrieval planner, `resolve_plugin_for_categories` for the extraction pack),
# so a placeholder string would be treated as a real category and would shadow
# the industry the caller declared on upload.
UNKNOWN_LABELS = {
    "other", "unknown", "unclassified", "uncategorized", "none", "null", "n/a", "na",
    "其他", "其它", "未知", "未分类", "无",
}


class DocumentClassifier:
    """Universal document classifier

    Collects classification taxonomy from loaded industry plugins, auto-classifies with LLM.
    If no industry plugins, uses a minimal generic taxonomy.
    """

    def __init__(self):
        self.model_client = get_model_client()

    def _build_taxonomy_prompt(self) -> str:
        """Build the candidate list the LLM chooses from.

        Candidates are each pack's ROUTING KEYS — its taxonomy names plus its
        manifest ``category_mapping`` — i.e. exactly the strings
        ``resolve_plugin_for_categories`` can match. Deriving candidates from
        taxonomy.yaml alone tied the vocabulary to which pack happened to ship
        that file: a pack with a category_mapping but no loaded taxonomy was
        invisible to the classifier, so documents it handles got force-fitted
        into whatever taxonomy *was* loaded (measured: every semiconductor
        datasheet classified as a financial announcement, and then routed to
        the financial pack for fact extraction).
        """
        registry = get_plugin_registry()
        plugins = registry.list_plugins()

        lines = ["Document classification taxonomy:"]
        for pid, info in plugins.items():
            taxonomy = info.get("taxonomy", {})
            mapping = [str(c).strip() for c in (info.get("categories") or []) if str(c).strip()]
            if not taxonomy and not mapping:
                continue
            l1 = taxonomy.get("level1") or info.get("name") or pid
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
            if mapping:
                lines.append(f"  Also accepts: {', '.join(mapping)}")

        if len(lines) == 1:
            lines.append("\n【General Documents】")
            lines.append("  Subcategories: Report, Manual, Contract, Paper, Announcement, Other")
            lines.append("\n【Sub-subcategory】Company/institution name or specific topic (extracted from document content)")

        lines.append(
            "\nIf the document does not fit any category above, do NOT pick the "
            "closest one: return empty strings for all three levels and a "
            "confidence below 0.5."
        )
        return "\n".join(lines)

    def classify(self, filename: str, title: str, content_sample: str,
                 plugin=None) -> dict[str, str]:
        """Three-level document classification.

        Directly calls the LLM; no content-related hardcoded rules in code. An
        industry plugin may supply its own classification prompt. A document the
        model cannot type is reported as unknown (all levels None) instead of
        being forced onto the closest category — the stored labels ROUTE
        downstream (industry pack selection, query-time category routing), so a
        forced label is a systematic misroute rather than a cosmetic mistake.
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
                        return self._normalize_classification(
                            result.get("category_level1"),
                            result.get("category_level2"),
                            result.get("category_level3"),
                            result.get("confidence", 0.5))
                except Exception as e:
                    logger.warning(f"[CLASSIFY] Industry plugin classification failed, falling back to generic classifier: {e}")
                # On failure, continue with generic classifier

        # Generic classifier (auto mode or when no industry plugin specified)
        taxonomy_text = self._build_taxonomy_prompt()
        system_prompt = (
            "你是一个文档分类助手。请根据文档信息提取分类，输出 ONLY JSON。"
            "三级分类尽量包含产品型号或公司名。"
            "若文档不属于任何候选类别，三个层级都返回空字符串，"
            "并把 confidence 设为 0.0-0.4，不要硬套最接近的类别。"
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
1. 一级分类: 若适用，从上述分类中选择一个；不适用则返回空字符串
2. 二级分类: 若适用，从选定一级下的子分类中选择一个；不适用则返回空字符串
3. 三级分类: 尽量包含产品型号或公司名称（从文件名和文本中提取实际出现的名称）
4. 置信度: 0.0-1.0（无法归类时给 0.0-0.4）

输出 ONLY JSON:
{{"category_level1": "...", "category_level2": "...", "category_level3": "...", "confidence": 0.0}}
"""

        try:
            result = self.model_client.generate_json(prompt, system_prompt=system_prompt, temperature=0.3, max_tokens=1024)
            if result and isinstance(result, dict):
                l1 = result.get("category_level1", "")
                l2 = result.get("category_level2", "")
                l3 = result.get("category_level3", "")
                # LLM may output old category names; dynamic mapping
                if l1 == "Financial & Transaction":
                    l1 = "Financial Reports"
                if l2 in ("Annual Report", "Quarterly Report", "Semi-annual Report", "Audit Report", "Prospectus", "Financial Statements"):
                    l1 = "Financial Reports"
                return self._normalize_classification(
                    l1, l2, l3, result.get("confidence", 0.5))
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
1. 一级分类: 若适用，从上述分类中选择一个；不适用则返回空字符串
2. 二级分类: 若适用，从选定一级下的子分类中选择一个；不适用则返回空字符串
3. 三级分类: 尽量包含产品型号或公司名称（从文件名和文本中提取实际出现的名称）
4. 置信度: 0.0-1.0（无法归类时给 0.0-0.4）

输出 ONLY JSON:
{{"category_level1": "...", "category_level2": "...", "category_level3": "...", "confidence": 0.0}}
"""

        try:
            result = self.model_client.generate_json(prompt, system_prompt=system_prompt, temperature=0.3, max_tokens=1024)
            if result and isinstance(result, dict):
                l1 = result.get("category_level1", "")
                l2 = result.get("category_level2", "")
                l3 = result.get("category_level3", "")
                if l1 == "Financial & Transaction":
                    l1 = "Financial Reports"
                if l2 in ("Annual Report", "Quarterly Report", "Semi-annual Report", "Audit Report", "Prospectus", "Financial Statements"):
                    l1 = "Financial Reports"
                return self._normalize_classification(
                    l1, l2, l3, result.get("confidence", 0.5))
        except Exception as e:
            logger.error(f"Document classification failed: {e}")

        # No usable answer from the model: report unknown rather than guessing a
        # type. A wrong type is worse than no type — it selects the wrong
        # industry pack for extraction and is then stored as if verified.
        return self._normalize_classification(None, None, None, 0.0)

    def _normalize_classification(self, level1, level2, level3, confidence) -> dict:
        """Normalize a classifier answer to the stored shape.

        Unknown collapses to None (SQL NULL) — never the literal
        "Other"/"Unknown": downstream consumers route on these fields, so a
        placeholder is indistinguishable from a real category. Levels below a
        missing level are dropped rather than fabricated (the old code filled
        level3 with a filename-derived product code).
        """
        levels = [self._clean_label(value) for value in (level1, level2, level3)]
        try:
            score = float(confidence)
        except (TypeError, ValueError):
            score = 0.0
        top = levels[0]
        if top is None:
            # Confidence in "no answer" is meaningless, and a non-zero value here
            # could slip past the routing floor.
            return {"category_level1": None, "category_level2": None,
                    "category_level3": None, "confidence": 0.0, "unknown": True}
        level2 = levels[1]
        level3 = levels[2] if level2 is not None else None
        return {"category_level1": top, "category_level2": level2,
                "category_level3": level3, "confidence": score, "unknown": False}

    @staticmethod
    def _clean_label(value) -> str | None:
        """Return a usable label, or None when the model said 'not classifiable'."""
        text = str(value).strip() if value is not None else ""
        if not text or text.lower() in UNKNOWN_LABELS:
            return None
        return text
