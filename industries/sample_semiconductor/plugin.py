"""
半导体行业包主插件（OpenLAD Hook 机制）

将原理图专用解析逻辑从 core 下沉到行业包，实现：
- 入库：detect_document_subtype + process_page + on_document_complete
- 检索：supplement_results + enhance_context

entry_point: plugin:SemiconductorPlugin
"""
import json
import logging
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any

# 先加载行业包子模块到 sys.path，确保 schematic 模块可导入
_PLUGIN_DIR = Path(__file__).parent
_SCHEMATIC_DIR = _PLUGIN_DIR / "ingestion" / "schematic"
if str(_SCHEMATIC_DIR) not in sys.path:
    sys.path.insert(0, str(_SCHEMATIC_DIR))

# 延迟导入 schematic 模块（避免在 sys.path 设置前导入）
_schematic_parser = None

def _get_schematic_parser(model_client=None):
    global _schematic_parser
    if _schematic_parser is None:
        from schematic_parser import SchematicParser
        _schematic_parser = SchematicParser(model_client=model_client)
    elif model_client is not None:
        _schematic_parser.model_client = model_client
    return _schematic_parser


def _load_yaml(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


# =============================================================================
# 加载 YAML 配置
# =============================================================================
_ingestion_prompts = _load_yaml(_PLUGIN_DIR / "ingestion" / "prompts.yaml")
_ingestion_rules = _load_yaml(_PLUGIN_DIR / "ingestion" / "rules.yaml")
_ingestion_skill = _load_yaml(_PLUGIN_DIR / "ingestion" / "skill.yaml")

_retrieval_prompts = _load_yaml(_PLUGIN_DIR / "retrieval" / "prompts.yaml")
_retrieval_rules = _load_yaml(_PLUGIN_DIR / "retrieval" / "rules.yaml")
_retrieval_constraints = _load_yaml(_PLUGIN_DIR / "retrieval" / "answer_constraints.yaml")
_retrieval_extraction = _load_yaml(_PLUGIN_DIR / "retrieval" / "extraction_rules.yaml")

_shared_glossary = _load_yaml(_PLUGIN_DIR / "shared" / "glossary.yaml")
_shared_taxonomy = _load_yaml(_PLUGIN_DIR / "shared" / "taxonomy.yaml")

logger = logging.getLogger(__name__)


# =============================================================================
# 入库插件
# =============================================================================
class SemiconductorIngestionPlugin:
    """半导体入库插件：原理图检测 + 结构化解析"""

    def __init__(self, model_client=None):
        self.model_client = model_client
        self._schematic_parser = None

    # === 已有接口（兼容 YAML 配置） ===
    def classify_document(self, content_sample: str, filename: str) -> Dict[str, Any]:
        return {}

    def extract_entities(self, page_text: str, page_type: str) -> List[Dict[str, Any]]:
        return []

    def get_ingestion_prompts(self) -> Dict[str, str]:
        return _ingestion_prompts

    def get_parsing_rules(self) -> Dict[str, Any]:
        return _ingestion_rules

    def enhance_summary(self, raw_summary: str, doc_metadata: Dict) -> str:
        return raw_summary

    def get_skill_config(self) -> Optional[Dict[str, Any]]:
        return _ingestion_skill if _ingestion_skill else None

    # === OpenLAD Hook 接口 ===
    def detect_document_subtype(self, parsed_doc: Any) -> Optional[str]:
        """检测文档子类型：原理图 / 其他"""
        if not parsed_doc:
            return None
        filename = (getattr(parsed_doc, 'filename', '') or '').lower()
        # 文件名特征
        if any(s in filename for s in ['_sch.', '_schematic.', '.sch.', 'schematic.']):
            return 'schematic'
        # 元数据特征
        title = str(getattr(parsed_doc, 'metadata', {}).get('title', '')).lower()
        if any(s in title for s in ['schematic', 'circuit', 'sch']):
            return 'schematic'
        # 内容特征：扫描前3页文本
        sample_text = ''
        for p in getattr(parsed_doc, 'pages', [])[:3]:
            sample_text += (getattr(p, 'raw_text', '') or '')[:2000]
        if not sample_text:
            return None
        component_refs = re.findall(r'\b([RCUQLDBVFM]\d{3,})\b', sample_text)
        net_labels = re.findall(r'(?:^|\s)(VCC|VDD|VSS|GND|AVDD|DVDD|RVDD|MVDD|3V3|1V8|5V)(?:_|\s|$)', sample_text)
        if len(component_refs) >= 10 and len(net_labels) >= 1:
            logger.debug(f"[SEMICONDUCTOR] 原理图内容特征匹配: {len(component_refs)} 元件, {len(net_labels)} 网络")
            return 'schematic'
        return None

    def process_page(self, page: Any, raw_text: str,
                     layout_result: Any = None,
                     model_client: Any = None,
                     page_image: Any = None) -> Optional[Dict[str, Any]]:
        """解析原理图页面结构化数据"""
        doc_subtype = getattr(page, '_doc_subtype', None)
        if doc_subtype != 'schematic':
            return None

        if not self._schematic_parser:
            self._schematic_parser = _get_schematic_parser(model_client)
        if not self._schematic_parser:
            return None

        try:
            sp = self._schematic_parser.parse_page(
                page_num=page.page_num,
                page_text=raw_text,
                page_title=getattr(page, 'section_title', '') or "",
                page_image=page_image
            )
            return {
                "subtype": "schematic",
                "data": sp.to_dict(),
                "searchable_text": sp.to_searchable_text(),
                "page_type_override": sp.page_type
            }
        except Exception as e:
            logger.warning(f"[SEMICONDUCTOR] 原理图解析失败 (页{page.page_num}): {e}")
            return None

    def on_document_complete(self, doc_id: str, metadata_db: Any) -> None:
        """文档完成回调（暂无跨页聚合需求）"""
        pass


# =============================================================================
# 检索插件
# =============================================================================
class SemiconductorRetrievalPlugin:
    """半导体检索插件：原理图补充召回 + 上下文增强"""

    def __init__(self):
        pass

    # === 已有接口（兼容 YAML 配置） ===
    def rewrite_query(self, query: str, chat_history: List[Dict] = None) -> str:
        return query

    def get_retrieval_prompts(self) -> Dict[str, str]:
        return _retrieval_prompts

    def get_answer_constraints(self) -> Dict[str, Any]:
        return _retrieval_constraints

    def disambiguate_terms(self, query: str) -> Dict[str, str]:
        result = {}
        for term, canonical in _shared_glossary.items():
            if term in query:
                result[term] = canonical
        return result

    def get_synonyms(self, term: str) -> List[str]:
        return []

    def format_citation(self, page_num: int, doc_title: str = "") -> str:
        return f"[^{page_num}^]"

    def post_process_answer(self, answer: str, context: str) -> str:
        return answer

    def get_section_boost_rules(self) -> Dict[str, Any]:
        return _retrieval_rules.get("section_boost_rules", {})

    def get_retrieval_rules(self) -> Dict[str, Any]:
        rules = dict(_retrieval_rules)
        if _retrieval_extraction:
            rules["exhaustive_extraction"] = _retrieval_extraction.get("exhaustive_extraction", {})
        return rules

    def get_query_expansion_keywords(self) -> List[str]:
        return _retrieval_rules.get("query_expansion", {}).get("keywords", [])

    def get_low_value_sections(self) -> List[Dict[str, Any]]:
        return _retrieval_rules.get("low_value_sections", [])

    def get_spec_sections(self) -> List[Dict[str, Any]]:
        return _retrieval_rules.get("spec_sections", [])

    # === OpenLAD Hook 接口 ===
    def supplement_results(self, query: str, results: list,
                           metadata_db: Any = None) -> list:
        """原理图结构化补充召回"""
        if not metadata_db:
            return []

        query_lower = query.lower()
        # FIX: 收紧电源查询判断，避免 npu/cpu/gpu/pin 等泛化硬件词误触发
        is_power_query = any(kw in query_lower for kw in
            ['电源', '供电', '电压', '电流', 'power', 'voltage', 'current',
             'pmic', 'dcdc', 'buck', 'ldo'])
        if not is_power_query:
            return []

        extra = []
        existing_keys = {(r.doc_id, r.page_num) for r in results}

        # FIX: 只扫描 results 中已有文档的 doc_id，避免污染不相关文档
        # 当查询 AB1234 规格表时，results 中只有 AB1234 datasheet，不会扫描 K7 原理图
        doc_ids_from_results = list({r.doc_id for r in results})

        # 如果 results 中没有含 extra_data 的文档，尝试获取所有含 extra_data 的文档
        # 但优先使用 results 中的 doc_id 以限制范围
        doc_ids = doc_ids_from_results if doc_ids_from_results else []
        if not doc_ids:
            try:
                with metadata_db.get_connection() as conn:
                    cur = conn.execute(
                        "SELECT DISTINCT doc_id FROM doc_pages WHERE extra_data IS NOT NULL OR schematic_data IS NOT NULL"
                    )
                    doc_ids = [r[0] for r in cur.fetchall()]
            except Exception as e:
                logger.warning(f"[SEMICONDUCTOR] 补充召回扫描失败: {e}")
                return []

        for doc_id in doc_ids:
            try:
                pages = metadata_db.get_document_pages(doc_id)
            except Exception:
                continue
            for p in pages:
                # 兼容 extra_data（新）和 schematic_data（旧）
                ed = p.get("extra_data") or p.get("schematic_data")
                if not ed or not isinstance(ed, dict):
                    continue

                # 新格式: {"subtype": "schematic", "data": {...}}
                # 旧格式: 直接是 schematic dict
                if ed.get("subtype") == "schematic":
                    sd = ed.get("data", {})
                else:
                    sd = ed

                page_type = sd.get("page_type", "")
                if page_type not in ("power_tree", "power_desc", "power_layout", "dcdc", "pinmux"):
                    continue

                sd_text = json.dumps(sd, ensure_ascii=False).lower()
                match_score = 0
                query_tokens = [t for t in query_lower.replace('?', '').replace('，', ' ').replace(',', ' ').split()
                                if len(t) > 2 and t not in {'example', 'sample'}]
                for token in query_tokens:
                    if token in sd_text:
                        match_score += 1
                if any(kw in sd_text for kw in ['vcc_core', 'vcc_io', 'vdd_soc']):
                    match_score += 3

                if match_score > 0:
                    pn = p.get("page_num", 0)
                    # OpenLAD FIX: 即使页面已在 results 中，也返回（让调用方决定是否更新分数）
                    # 这样 supplement_results 可以升级已有页面的分数
                    doc = metadata_db.get_document(doc_id)
                    page = metadata_db.get_page(p.get("id"))
                    if page and doc:
                        # 构造 SearchResult（与 core 中一致）
                        # 延迟导入，避免模块加载时包路径问题
                        import importlib
                        retriever_mod = importlib.import_module("core.retrieval.retriever")
                        SearchResult = getattr(retriever_mod, "SearchResult")
                        content = page.get("raw_text", "")
                        # 优先使用结构化文本
                        try:
                            from schematic_types import SchematicPage
                            sp = SchematicPage.from_dict(sd)
                            structured_text = sp.to_searchable_text()
                            if structured_text:
                                content = f"{structured_text}\n\n[原始文本片段]\n{content[:500]}"
                        except Exception:
                            pass

                        # OpenLAD FIX: 给原理图页面更高分数，确保在跨文档检索时不被规格书挤出
                        base_score = 3.0 if page_type in ("power_tree", "power_desc", "dcdc") else 2.0
                        sr = SearchResult(
                            doc_id=doc_id,
                            page_id=p.get("id"),
                            page_num=pn,
                            score=base_score + match_score * 1.0,
                            content=content,
                            section_title=p.get("section_title", ""),
                            filename=doc.get("filename", ""),
                            title=doc.get("title", ""),
                            extra_data=ed,
                        )
                        extra.append(sr)
                        existing_keys.add((doc_id, pn))
                        logger.info(f"[SEMICONDUCTOR] 原理图结构化召回: doc={doc_id[:8]} page={pn} type={page_type} score={sr.score:.1f}")

        return extra

    def enhance_context(self, query: str, context: str,
                        sources: List[Dict[str, Any]] = None) -> str:
        """增强上下文：将原理图结构化数据转换为 Markdown 表格"""
        return context


# =============================================================================
# 行业包主类
# =============================================================================
import sys
from pathlib import Path

# FIX: 动态导入 IndustryPlugin/IndustryManifest，确保与 PluginRegistry 中的类一致
_plugin_file = Path(__file__).resolve()
_project_root = _plugin_file.parent.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# 统一使用 core.plugins 模块，确保 issubclass 检查通过
import core.plugins as _plugins_module
IndustryPlugin = getattr(_plugins_module, "IndustryPlugin")
IndustryManifest = getattr(_plugins_module, "IndustryManifest")


class SemiconductorPlugin(IndustryPlugin):
    """半导体行业包主插件"""

    def __init__(self):
        self._manifest = IndustryManifest.from_yaml(_PLUGIN_DIR / "manifest.yaml")
        self._ingestion = SemiconductorIngestionPlugin()
        self._retrieval = SemiconductorRetrievalPlugin()

    @property
    def manifest(self) -> IndustryManifest:
        return self._manifest

    @property
    def ingestion(self) -> SemiconductorIngestionPlugin:
        return self._ingestion

    @property
    def retrieval(self) -> SemiconductorRetrievalPlugin:
        return self._retrieval
