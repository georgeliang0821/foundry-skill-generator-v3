"""
Skill Gatekeeper v2.6.1 - Skills-Aware 本地 skills/ 目錄寫回
====================================================================
與 code_agent_hosted.py 配合使用（由 trigger_gatekeeper_async() 觸發）。
CodingAgent 成功執行後，非同步觸發 Gatekeeper 進行 Skill 寫回。

版本策略：
- v1 (舊版): 寫回 AI Search + Blob Storage（已棄用）
- v2 (前版): 寫回本地 skills/ 目錄，讓 SkillsProvider 自動 discover
- v2.1: 新增 skills-aware 邏輯，智慧跳過/合併
- v2.6.1 (本版): Phase 1 boolean gate + HITL 審核 + SQL 同步 contract

現行範圍：
- Phase 1 知識閘門 _has_extractable_knowledge()（boolean，非四分類 scoring）
- Type B 知識萃取（用 GatekeeperAgent LLM 呼叫）
- 本地 skills/ 目錄寫回（符合 Agent Skills 規範）
- 簡化查重（比對現有 skill name/description）
- Skills-aware 智慧跳過/合併（relevance 評分挑選最佳合併目標）
- HITL：SKILL_REVIEW_MODE=manual 時改寫入 Blob pending 待人工審核

P1 預留（未來擴充）：
- LLM 層分類（深度泛化性判斷）
- Type A 工具型保存
- Type C 模板抽象化

VERSION: 2.8
2026.08.24 George: v2.8 — merge 基準改以 Blob 為真實來源 + skills_referenced 解析失敗護欄
- 根因:本檔所有查找與 merge 都踩在本地 SKILLS_DIR 上,而那只是「本 replica
  啟動時的快照」;Mode B 的讀路徑則是 Blob。多 replica 或人工發布後不重啟,
  兩邊必然分岔。
- 新增 _resolve_and_refresh_skill_dir():先從 Blob 抓目標 skill 的 SKILL.md
  寫回本地再 merge,並帶回 ETag。命中也一律 refresh —— _is_orchestration_skill()、
  check_relevance_for_merge()、_increment_version()、_extract_existing_error_keys()
  全部讀同一份內容,只要基準是舊的,這四個就全部一起錯。
- GatekeeperDecision 新增 skill_md_etag,供呼叫端做 If-Match 條件上傳。
- PHASE 3 護欄:skills_referenced 有值卻一個都解析不到時,不再 fall through 到
  check_duplicate()。此時正確目標依定義不在候選集內,而 _tokenize 沒有 stopword
  過濾、門檻又只有 3 個 token,同領域命名(azure-*、ms-graph-*)很容易互撞
  —— 命中的必然是別人。auto 直接放棄寫回,manual 仍轉 pending 交人判斷。
- 本次不處理私有 skill 的誤併(loaded_skills 只帶 skill_name、不帶 skill_key),
  私有 skill 幾乎不含 python code,refine 需求極低。

VERSION: 2.7
2026.08.21 George: v2.7 — 編排層 skill 不接受知識寫回(Skill Registry 分階配套)
- 新增 _is_orchestration_skill():以 yaml.safe_load 讀 frontmatter,判斷是否
  宣告了 metadata.children。刻意不重用 _parse_skill_frontmatter() —— 它是
  手寫逐行 parser,`children:` 接 block-style list 時只會拿到空字串。
- PHASE 3 查重的兩條路徑都排除編排層:
  (a) skills_referenced 候選迴圈 —— 分階情境下 loaded_skills 同時含 parent 與
      child,而 parent 的 tags/description 跟錯誤情境高度相關,relevance score
      很容易贏過真正出錯的 child,Addendum 就會靜靜寫進編排層並自動同步回 Blob
  (b) check_duplicate() 的一般查重 —— 命中編排層時落回「開新 skill」
- 已知限制:child 的 input_contract 若被 refine 改動,parent 正文的描述不會
  跟著更新,需人工對帳。初始一致性靠 parent/child 共同撰寫時確保。

VERSION: 2.6.1
2026.08.15 George: v2.6.1 檔頭校正 + 補記 2026.08.14 SQL 同步 contract
- 補記 2026.08.14 的變更（原本只散落在程式碼註解，檔頭完全沒提）：
  - GatekeeperDecision 新增 skill_name 欄位（Optional[str]）
  - run_gatekeeper() 回傳時 skill_name 取 os.path.basename(skill_dir)，
    而非 knowledge["skill_name"]：merge 情境實際落地的是既有 skill 的目錄名，
    取後者會在 SQL 多開一列根本不存在的 skill
  - Post-processing contract：auto 模式下由呼叫端（code_agent_hosted.py 的
    _sync_gatekeeper_skill_to_sql）在 Gatekeeper 回傳後，依 action_taken 與
    skill_name 背景同步到 Azure SQL；本模組本身不碰 SQL
- 補記 v2.6 HITL 用到但檔頭未列的常數：
  PENDING_BLOB_PREFIX（pending 知識的 Blob 前綴）、
  LOGIC_APP_SKILL_REVIEW_URL（審核通知 Logic App trigger URL）
- 刪除死碼：舊版回傳 bool 的 check_relevance_for_merge() 註解區塊（78 行），
  已被 v2.4.1 的 float 版取代，無保留價值
- check_relevance_for_merge() 中間過程的 9 行 logger.info 降為 logger.debug：
  該函式在 PHASE 3 對每個候選 skill 各跑一次，原本 O(n)×9 洗版，
  只保留最後 PASS/FAIL 一行 info（評分邏輯、閾值、回傳值完全未動）
- 檔頭校正（一律以程式碼為準）：標題版本、caller 檔名、P0 範圍描述、
  classify_by_rules 去向、v2.4.1 的三個上限值

2026.03.30 George: v2.6.0 Phase 1 簡化 + Human-in-the-Loop (HITL) Skill Review
- Phase 1 簡化（選項 A：boolean gate）：
  - 問題：classify_by_rules() 費力計算 tool/knowledge/pattern/artifact 四個 scores，
    但 P0 下只有 has_knowledge_to_extract（= error_count >= 1）被消費。
    primary_type、confidence、suggested_action 全部只進 log/reason，不影響任何分支。
  - 修正：用 _has_extractable_knowledge() 一行 boolean 取代整個四分類 scoring。
    ⚠️ 2026.08.15 更正敘述：classify_by_rules() 及其 patterns 常量是整支移除，
    並未保留註解區塊（P1 需要時重新設計）。
  - ClassificationResult 簡化為固定值填充（保持 GatekeeperDecision 結構相容）。
  - 移除 HARDCODED_PATTERNS / PARAMETERIZED_PATTERNS / REPEATED_STRUCTURE_PATTERNS
    等常量（P1 時重新設計）。
- HITL Skill Review：
  - 新增 SKILL_REVIEW_MODE env var（auto | manual）
    - auto: 維持現有行為，PHASE 4 直接寫入正式 skills/ 目錄
    - manual: PHASE 4 改為寫入 Blob pending → 通知 Logic App → 人類審核
  - 新增 GatekeeperAction.PENDING_REVIEW enum value
  - 新增 GatekeeperDecision.pending_id 欄位（Optional[str]）
  - 新增 _write_to_pending(): 將 knowledge JSON + 決策 context 序列化到 Blob
  - 新增 _notify_logic_app(): HTTP POST Logic App trigger URL
  - PHASE 4 加入 SKILL_REVIEW_MODE 分支，manual 時不呼叫 write_knowledge_skill()
  - 設計決策：pending 存 knowledge JSON 而非最終 SKILL.md，
    approve 時才對當下最新正式 skill 做 merge，避免後蓋前問題
  - trigger_gatekeeper_async() 新增 PENDING_REVIEW 的控制台輸出

2026.03.27 George: v2.5.0 Workflow Pattern 萃取
- 問題：Gatekeeper 只能捕捉「錯誤→解法」(known_issues)，無法捕捉
  CodingAgent 從失敗到成功過程中自行發展出的「工作流程 pattern」。
  例如 diagrams 套件的 case：agent 在第 2 次嘗試中自發加入了 pkgutil
  探測邏輯，但這個「先探測再寫 code」的 pattern 無法被現有資料結構記錄，
  導致自動產生的 skill 只剩下 placeholder 級別的 solution。
- 修正 1: KNOWLEDGE_EXTRACTION_PROMPT 新增 workflow_patterns JSON 欄位
  - title: Pattern 名稱
  - trigger: 什麼情境下應套用此 pattern
  - steps: 具體步驟（含可執行的程式碼片段）
  - rationale: 為什麼需要此 pattern、不做會怎樣
- 修正 2: LLM prompt 新增「行為轉變觀察」hint
  - 引導 LLM 比對 v1/v2/v3 之間的差異，從失敗→成功的轉變中抽出 pattern
  - 強調 steps 必須具體到可以直接複製貼上執行
- 修正 3: _create_new_skill() 新增 ## 建議工作流程 section 渲染
- 修正 4: _merge_gatekeeper_auto() 和 _merge_manual_skill() 支援
  workflow_patterns 的合併寫入
- 修正 5: extract_knowledge_by_rules() fallback 輸出新增空
  workflow_patterns 欄位（保持結構一致性）
- 修正 6: _format_workflow_patterns_section() 抽出為共用渲染函式
- 設計決策：workflow_patterns 不做 dedup（不像 error_pattern 有結構化 key，
  pattern 的 title/steps 每次 LLM 生成措辭不同，用字串比對會漏判。
  透過 known_issues dedup 間接控制重複觸發頻率即可）

2026.03.10 George: v2.4.1 Hotfix — LLM prompt 超限修正
- 問題：v2.4 的 existing_skills_catalog 將所有 skills 的 frontmatter 塞進 LLM prompt，
  加上 final_code[:4000] + errors_text + skills_context，總長度超過 Gatekeeper 模型的
  input token limit，導致 Azure AI Foundry Responses API 回 error（HTTP 200 但 body
  帶 "There was an issue with your request"），LLM 萃取每次都失敗
- 修正 1: _collect_existing_skills_summary() 縮減輸出量
  - description 截斷從 200 字 → 80 字（LLM 只需判斷主題範圍）
  - 最多 skills 數從 30 → 20
  - 新增總輸出字元上限 1500 chars，超過時截斷到最後完整 entry
  - ⚠️ 2026.08.15 更正：以上三個值後續已放寬且未記錄，現行程式碼為
    MAX_DESC_CHARS = 200 / MAX_SKILLS_IN_PROMPT = 30 / MAX_TOTAL_CHARS = 5000
    （下方修正 2 的 PROMPT_MAX_CHARS = 20000 則仍相符）
- 修正 2: extract_knowledge_with_llm() 新增 prompt 長度安全檢查
  - 安全上限 PROMPT_MAX_CHARS = 20000（≈ 6666-10000 tokens）
  - 漸進式裁剪策略：
    Step 1: 移除 existing_skills_catalog（影響最小）
    Step 2: 縮短 final_code 為 2000 chars（保留核心邏輯）
  - 新增 prompt 長度 log（方便未來 debug token 問題）
- 修正 3: PHASE 3 skills_referenced 合併目標選擇邏輯重寫
  - 問題：原迴圈對 skills_referenced 列表取第一個找到的 skill 就 break，
    不管 relevance 高低。例如 ['azure-ad-...', 'outlook-...', 'azure-diagrams-tips']
    中，diagrams 相關的知識被合併到第一個碰巧 PASS 的 azure-ad skill
  - 修正：遍歷所有 skills_referenced，對每個做 relevance 評分，選分數最高的
  - check_relevance_for_merge() 回傳值從 bool → float (0.0-1.0)
    - Tags meaningful overlap: 0.4 分
    - Description keyword overlap: 0.3 分
    - Package overlap: 0.3 分
    - 向後相容：score > 0 等同原本的 True
- 修正 4: extract_knowledge_with_llm() LLM 呼叫加入逐步 log
  - 問題：LLM streaming call 如果失敗或卡住，只有一行 "LLM prompt 長度" log，
    完全無法判斷卡在哪一步（建立 stream? 接收 chunks? 解析回應?）
  - 修正：在每個關鍵步驟加 log —
    "LLM 開始呼叫" → "LLM stream 已建立" → "LLM streaming 完成 (N chunks, M chars)"
  - except 中加入 exception type name（原本只有 message，現在有 type(e).__name__）
  - 不加 timeout：Gatekeeper 已與主流程切割，寧可跑完拿到完整結果和 log

2026.03.10 George: v2.4 Issue-Level Dedup — 修正 SKILL.md 無限長大問題
- 問題：_merge_gatekeeper_auto() 和 _merge_manual_skill() 在 append known_issues 時
  完全沒有比對新 issue 是否已存在於現有 SKILL.md，導致相同問題（如 ModuleNotFoundError:
  No module named 'diagrams'）每次執行都重複寫入，SKILL.md 持續膨脹
- 修正 1: 新增 _normalize_error_key() 函式，從 error_pattern 提取 normalized key
  （ErrorType + 引號內關鍵字），用於跨次 merge 的精確比對
  - 例："ModuleNotFoundError: No module named 'diagrams'" → "modulenotfounderror:diagrams"
  - 例："ImportError: cannot import name 'DataFactory' from 'diagrams.azure.integration'"
    → "importerror:datafactory:diagrams.azure.integration"
- 修正 2: 新增 _extract_existing_error_keys() 函式，從現有 SKILL.md 的
  **錯誤訊息**: `...` 行中批次提取所有已記錄的 normalized key
- 修正 3: 新增 _dedup_known_issues() 函式，統一 issue-level 去重邏輯，
  供 _merge_gatekeeper_auto() 和 _merge_manual_skill() 共用
  - 比對策略（任一命中即視為重複）：
    a. error_pattern normalized key 完全匹配
    b. issue title 已存在於現有內容（防 error_pattern 微調繞過）
  - 同批次內也做 key 累積，防止同一次萃取出的重複 issue 都寫入
- 設計決策：不使用 embedding/similarity，因為 error_pattern 已高度結構化
  （Python ErrorType + message），normalized key 比對即可覆蓋絕大多數情境，
  且零外部依賴、毫秒級完成，符合 Gatekeeper graceful degradation 原則
- 修正 5: 重寫 _extract_tags()，移除無法維護的 hardcoded tech_keywords 列表
  - 改從兩個動態來源提取 tags：
    a. _extract_package_names(final_code) — import 語句中的第三方套件（最可靠）
    b. user_request 中的有意義英文關鍵字（補充非套件名的技術概念）
  - 效果：零維護成本，新套件/技術自動被收錄到 tags
- 修正 6: 新增 _collect_existing_skills_summary()，在 LLM 知識萃取 prompt 中
  注入所有現有 skills 的 frontmatter（name + description），讓 LLM 在產出
  skill_name 時能直接對齊已存在的 skill，解決同概念被不同名稱重複建立的問題
  - 無論 skills_referenced 有無值都注入（擴展原 v2.1 只在 skills_referenced
    有值時才帶 context 的限制）
  - 共用同一次 LLM call，零額外成本
  - KNOWLEDGE_EXTRACTION_PROMPT 新增 {existing_skills_catalog} 佔位符和
    skill_name 命名規則提示

2026.03.05 George: v2.3 通用化 SKILL.md 格式支援
- 問題：merge 邏輯原先綁定 Gatekeeper 自產的固定 heading（**相關套件**、## 已知問題與 Workaround、
  ## ⚠️ 注意事項），導致人工撰寫的 skill（如 outlook-calendar-management）無法正確合併
- 修正 1: _detect_skill_origin() 新增函式，透過 metadata.author 或結構特徵偵測
  skill 來源（gatekeeper-auto vs manual），分流不同的 merge 策略
- 修正 2: _merge_into_existing_skill() 對 manual skill 使用獨立的
  「## Gatekeeper Addendum」section 進行追加，不破壞原有人工編排的結構
- 修正 3: _parse_version_string() 新增函式，同時相容 semver（1.0.0）和
  簡化版（"1.0"）兩種版本號格式，修復版本遞增在非標準格式下靜默失敗的問題
- 修正 4: _parse_skill_frontmatter() 擴充，額外解析 version 和 metadata.author
  欄位，為 origin detection 提供資訊

2026.03.05 George: v2.2 修正 _merge_into_existing_skill 的 useful_packages 資訊斷層
- merge 模式下將新 packages union 進現有「相關套件」行
- 若原 SKILL.md 缺少「相關套件」行，自動插入到「## 已知問題」前
- 修復 check_relevance_for_merge 維度 3 (Package 重疊) 在多次 merge 後逐漸失準的問題

2026.03.04 George: v2.2 Relevance Check 升級
- 新增 check_relevance_for_merge() 函式
  - 在 PHASE 3 skills-aware merge 前驗證新知識與目標 skill 的相關性
  - 比對維度：tags 重疊、description 關鍵字重疊、packages 重疊
  - 排除太泛的關鍵字（azure, python, api 等）避免誤命中
  - 任一維度達到閾值即通過，全部不通過則跳過合併、走一般流程
- 解決問題：CodingAgent 因泛關鍵字誤匹配 skill（如 DALL-E 任務匹配到 diagrams skill）
  導致不相關知識被合併回錯誤的 skill
- 新增 GatekeeperAction.SKIP_MERGE_NOT_RELEVANT
- 新增設定值：RELEVANCE_MIN_TAG_OVERLAP, RELEVANCE_MIN_KEYWORD_OVERLAP

2026.02.28 George: v2.1 Skills-Aware 升級
- GatekeeperPayload 新增 skills_referenced 欄位
- run_gatekeeper() 新增 skills-aware 決策邏輯：
  - 有參考 skills + 無 error → 完全跳過（skills 發揮作用）
  - 有參考 skills + 有 error → 優先合併回已參考的 skill（正向回饋迴路）
  - 無參考 skills → 正常走完整流程
- trigger_gatekeeper_async() 新增 skills_referenced 參數
- from_conversation_state() 支援接收 skills_referenced

2026.02.28 George: v2.0 重寫 - 改為本地 FileAgentSkillsProvider 寫回
"""

import asyncio
import json
import os
import re
import logging
import hashlib
import yaml     # 2026.08.21 George : _is_orchestration_skill() 需要真正的 YAML parser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from enum import Enum

# 2026.03.30 George: v2.6 HITL — 新增 imports
import aiohttp     # HTTP POST to Logic App
import uuid        # pending_id 產生

logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION
# ============================================================================

# Skills 目錄（與主程式共用同一個設定）
SKILLS_DIR = os.environ.get("SKILLS_DIR", os.path.join(os.getcwd(), "skills"))

# 守門閾值
MIN_ERRORS_FOR_KNOWLEDGE = 1    # 至少 N 個 error 才萃取知識
DEDUP_SIMILARITY_KEYWORDS = 3   # 至少 N 個相同關鍵字才視為重複

# 2026.03.04 George: v2.2 Relevance Check 閾值
# skills_referenced 合併前，驗證新知識與目標 skill 的相關性
RELEVANCE_MIN_TAG_OVERLAP = 2       # 至少 N 個 tag 重疊才視為相關
RELEVANCE_MIN_KEYWORD_OVERLAP = 3   # 至少 N 個 description 關鍵字重疊才視為相關


# ============================================================================
# DATA MODELS
# ============================================================================

class SkillType(str, Enum):
    """Skill 產出物的四種分類"""
    TOOL = "tool"               # Type A: 工具型，可直接複用
    KNOWLEDGE = "knowledge"     # Type B: 知識型，踩坑經驗
    PATTERN = "pattern"         # Type C: 模式型，需抽象化為模板
    ARTIFACT = "artifact"       # Type D: 產出物型，不適合保存


class GatekeeperAction(str, Enum):
    """Gatekeeper 的處理動作"""
    SAVE_TOOL = "save_tool"                 # 完整保存 PY + MD (P1)
    UPDATE_KNOWLEDGE = "update_knowledge"   # 更新或新建知識型 MD (P0 ✓)
    CREATE_TEMPLATE = "create_template"     # 抽象化後保存模板 + MD (P1)
    SAVE_EXAMPLE = "save_example"           # 僅存為範例
    SKIP = "skip"                           # 不保存
    # 2026.02.28 George: v2.1 新增 — 有參考 skills 且無 error 時使用
    SKIP_SKILLS_EFFECTIVE = "skip_skills_effective"  # Skills 已發揮作用，無需萃取
    # 2026.03.04 George: v2.2 — skills_referenced 與新知識不相關，跳過合併
    SKIP_MERGE_NOT_RELEVANT = "skip_merge_not_relevant"
    # 2026.03.30 George: v2.6 HITL — 知識已萃取但等待人工審核
    PENDING_REVIEW = "pending_review"


@dataclass
class GatekeeperPayload:
    """
    CodingAgent 成功執行後，傳給 Gatekeeper 的資料包。
    從 ConversationState 中萃取必要資訊。
    
    2026.02.28 George: v2.1 新增 skills_referenced 欄位
    """
    task_id: str                            # session_id
    user_request: str                       # 使用者的原始需求
    final_code: str                         # 最終成功執行的 Python 程式碼
    execution_history: List[Dict]           # 執行歷程（含 try-error）
    output_type: str                        # 產出類型描述
    error_messages: List[str]               # 萃取出的錯誤訊息列表
    # 2026.02.28 George: v2.1 CodingAgent 本次參考了哪些 skills
    skills_referenced: List[str] = field(default_factory=list)

    @classmethod
    def from_conversation_state(
        cls,
        state,
        user_request: str,
        skills_referenced: List[str] = None,
    ) -> "GatekeeperPayload":
        """
        從 ConversationState 建立 Payload。
        銜接點：state 是主程式的 ConversationState 物件。
        
        2026.02.28 George: v2.1 新增 skills_referenced 參數
        """
        execution_history = []
        error_messages = []
        attempt = 0

        for msg in state.messages:
            author = getattr(msg, 'author_name', None)
            if author == "CodeExecutor":
                attempt += 1
                # v8.1: Message.text 或 msg.contents[0] 取文字
                text = ""
                if hasattr(msg, 'text') and msg.text:
                    text = msg.text
                elif hasattr(msg, 'contents') and msg.contents:
                    text = str(msg.contents[0])

                is_success = "✅" in text
                execution_history.append({
                    "attempt": attempt,
                    "success": is_success,
                    "output_snippet": text[:500],
                })
                if not is_success:
                    error_messages.append(text[:1000])

        # 讀取最終腳本內容
        final_code = ""
        if state.final_script_path and os.path.exists(state.final_script_path):
            with open(state.final_script_path, "r", encoding="utf-8") as f:
                final_code = f.read()

        # 推斷產出類型
        output_type = "code_script"
        if state.output_files:
            extensions = [os.path.splitext(f)[1].lower() for f in state.output_files]
            if any(ext in ['.png', '.jpg', '.svg'] for ext in extensions):
                output_type = "chart_or_diagram"
            elif any(ext in ['.csv', '.xlsx', '.json'] for ext in extensions):
                output_type = "data_file"
            elif any(ext in ['.html', '.pdf'] for ext in extensions):
                output_type = "document"

        return cls(
            task_id=state.session_id,
            user_request=user_request or "",
            final_code=final_code,
            execution_history=execution_history,
            output_type=output_type,
            error_messages=error_messages,
            skills_referenced=skills_referenced or [],
        )


@dataclass
class ClassificationResult:
    """規則層分類結果"""
    primary_type: SkillType
    confidence: float
    has_knowledge_to_extract: bool
    scores: Dict[str, float]
    suggested_action: GatekeeperAction


@dataclass
class GatekeeperDecision:
    """Gatekeeper 最終決策"""
    task_id: str
    classification: ClassificationResult
    action_taken: GatekeeperAction
    skill_written: Optional[str]            # 寫入的 skill 目錄路徑（None = 未寫入）
    reason: str
    processing_time_ms: int
    # 2026.03.30 George: v2.6 HITL — PENDING_REVIEW 時帶回 pending_id 供 callback 使用
    pending_id: Optional[str] = None
    # 2026.08.14 George: auto 模式背景同步 SQL 時需要，避免呼叫端再從路徑反推
    skill_name: Optional[str] = None
    # 2026.08.24 George: merge 基準那份 SKILL.md 在 Blob 上的 ETag。呼叫端拿它做
    # If-Match 條件上傳;None = 新建、或基準不是從 Blob 來的，走無條件覆蓋。
    skill_md_etag: Optional[str] = None


# ============================================================================
# PHASE 1: KNOWLEDGE GATE（P0 — 簡化為 boolean）
# 2026.03.30 George: v2.6 選項 A 簡化
#
# P0 下只實作 Type B（knowledge），classify_by_rules() 的四分類 scoring
# 從未被消費（primary_type/confidence/suggested_action 只進 log），
# 唯一影響流程的欄位是 has_knowledge_to_extract = (error_count >= 1)。
#
# 因此以 _has_extractable_knowledge() boolean gate 取代整個 scoring 邏輯。
# classify_by_rules() 及其 patterns 常量已整支移除，P1 需要時重新設計。
# ============================================================================


def _has_extractable_knowledge(payload: "GatekeeperPayload") -> bool:
    """
    Phase 1 簡化：判斷 payload 中是否有可萃取的知識。
    P0 範圍下等價於「有沒有執行錯誤」。
    """
    return len(payload.error_messages) >= MIN_ERRORS_FOR_KNOWLEDGE


def _make_gate_classification(has_knowledge: bool) -> "ClassificationResult":
    """
    為 GatekeeperDecision 產生一個最小化的 ClassificationResult。
    保持結構相容，但不做任何 scoring。
    """
    return ClassificationResult(
        primary_type=SkillType.KNOWLEDGE,
        confidence=1.0 if has_knowledge else 0.0,
        has_knowledge_to_extract=has_knowledge,
        scores={"knowledge": 1.0 if has_knowledge else 0.0},
        suggested_action=(
            GatekeeperAction.UPDATE_KNOWLEDGE if has_knowledge
            else GatekeeperAction.SKIP
        ),
    )


# ============================================================================
# PHASE 2: KNOWLEDGE EXTRACTION（Type B 知識萃取 — P0）
# ============================================================================

# 2026.03.10 George: v2.4 新增 — 收集現有 skills 的 frontmatter 摘要
# ============================================================================
# 用途：在 LLM 知識萃取的 prompt 中提供現有 skills 的 name + description，
# 讓 LLM 在產出 skill_name 時能直接對齊到已存在的 skill，而非每次自創新名。
#
# 這解決了 LLM 每次萃取可能產出不同 skill name 的問題（例如同一個概念被
# 命名為 azure-diagrams / diagram-lib / python-diagrams），且不需要額外的
# LLM call 或 embedding 基礎設施 — 摘要直接塞進已有的萃取 prompt 中。
# ============================================================================

def _collect_existing_skills_summary(skills_dir: str = None) -> str:
    """
    2026.03.10 George: v2.4 新增
    2026.03.10 George: v2.4.1 修正 — 縮減 description 長度，避免 prompt 超限
    
    掃描 skills/ 目錄，收集所有 SKILL.md 的 frontmatter 摘要（name + description）。
    回傳格式化的字串，供 LLM prompt 使用。
    
    設計考量：
    - 只讀取 frontmatter（--- 之間的區塊），不讀全文
    - 若 skills/ 目錄不存在或為空，回傳空字串（graceful degradation）
    - 單個 skill 的 description 截斷為前 200 字（v2.4.1:
      因為 LLM 只需要足夠資訊判斷 skill 的主題範圍，不需要完整描述）
    - 最多收集 30 個 skills
    - 總輸出上限 5000 字元（v2.4.1 新增），超過時截斷到最後一個完整 skill entry
      → 確保 catalog 不會吃掉太多 prompt token budget
    
    Returns:
        格式化的 skills 摘要字串，如果沒有任何 skill 回傳 ""
    """
    skills_dir = skills_dir or SKILLS_DIR
    if not os.path.exists(skills_dir):
        return ""
    
    MAX_SKILLS_IN_PROMPT = 30
    MAX_TOTAL_CHARS = 5000       # v2.4.1: 總輸出字元上限
    MAX_DESC_CHARS = 200          # v2.4.1: 單個 description 字元上限
    
    summaries = []
    total_chars = 0
    
    # 按目錄名排序，確保結果穩定
    for item in sorted(os.listdir(skills_dir))[:MAX_SKILLS_IN_PROMPT]:
        skill_md_path = os.path.join(skills_dir, item, "SKILL.md")
        if not os.path.isfile(skill_md_path):
            continue
        
        try:
            with open(skill_md_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            name, description, _ = _parse_skill_frontmatter(content)
            if not name:
                continue
            
            # 截斷過長的 description
            desc_display = description[:MAX_DESC_CHARS]
            if len(description) > MAX_DESC_CHARS:
                desc_display += "..."
            
            entry = f"- name: {name}\n  description: \"{desc_display}\""
            
            # v2.4.1: 檢查總長度上限
            if total_chars + len(entry) > MAX_TOTAL_CHARS:
                logger.info(
                    f"[Gatekeeper] Skills catalog 達到長度上限 ({total_chars} chars), "
                    f"截斷於 {len(summaries)} 個 skills"
                )
                break
            
            summaries.append(entry)
            total_chars += len(entry) + 2  # +2 for \n\n separator
        
        except Exception:
            continue
    
    if not summaries:
        return ""
    
    return "\n\n".join(summaries)

KNOWLEDGE_EXTRACTION_PROMPT = """你是一個 Skill Gatekeeper，負責從 CodingAgent 的執行歷程中萃取可複用的技術知識。

## 使用者的原始需求
{user_request}

## 最終成功的程式碼（前 2000 字）
```python
{final_code}
```

## 執行過程中遇到的錯誤
{errors_text}

{skills_context}

{existing_skills_catalog}

## 任務
請從上述執行歷程中萃取「其他開發者未來可能也會遇到」的技術知識。
{merge_hint}

回傳 JSON 格式（只回傳 JSON，不要加其他文字）：
{{
  "skill_name": "<簡短的 skill 名稱，用小寫英文和連字號，如 azure-diagrams-tips>",
  "skill_description": "<一句話描述這個 skill 包含什麼知識，最多 200 字>",
  "known_issues": [
    {{
      "title": "<問題標題>",
      "error_pattern": "<會看到的錯誤訊息片段>",
      "root_cause": "<根因>",
      "solution": "<解法>",
      "applicable_when": "<什麼情況下適用>"
    }}
  ],
  "workflow_patterns": [
    {{
      "title": "<Pattern 名稱，如 Pre-flight class discovery>",
      "trigger": "<什麼情境下應該套用此 pattern>",
      "steps": "<具體步驟，用 1. 2. 3. 條列，必須包含可直接執行的程式碼片段>",
      "rationale": "<為什麼需要這個 pattern，不做會怎樣>"
    }}
  ],
  "useful_packages": ["<本次用到的關鍵套件名稱>"],
  "tags": ["<只填寫 3-8 個與技術棧直接相關的關鍵字（如套件名、Azure 服務名、程式語言、框架名）。不要填入任何業務用語（如公司名、產品名）或通用動詞/形容詞。>"]
}}

## 規則
- 只萃取「通用且可複用」的知識，不要記錄使用者特有的業務邏輯
- error_pattern 要寫出實際的錯誤訊息關鍵字，讓未來比對時能命中
- 如果沒有有價值的知識可萃取（例如只是 typo），回傳 {{"skip": true, "reason": "..."}}
- ★ skill_name 命名規則（重要）：如果「系統中已有的 Skills」清單中有 description 與本次知識相關的 skill，請直接使用該 skill 的 name 作為 skill_name，以便知識合併回現有 skill 而非新建。只有在確定沒有任何已有 skill 適合時，才新建名稱。
- ★ workflow_patterns 萃取規則（重要）：
  - 觀察 CodingAgent 從失敗到成功的「行為轉變」— 它在失敗時做了什麼，成功時改成做什麼？
  - 典型的 workflow pattern 包括：「先探測/先列舉/先查詢可用資源，再開始實作」、「先驗證環境/版本，再寫業務邏輯」、「先用小範圍測試確認 API 行為，再擴展到完整實作」
  - steps 必須具體到可以直接複製貼上執行的程度，包含程式碼片段（用 markdown code block）
  - 如果執行歷程中沒有明顯的行為轉變（例如只是修 typo 或補漏參數），workflow_patterns 應為空陣列 []
  - 不要把單純的「錯誤修正」包裝成 workflow pattern — 只有「策略性的工作流程改變」才算
"""


async def extract_knowledge_with_llm(
    payload: GatekeeperPayload,
    gatekeeper_agent: Any,
) -> Optional[Dict]:
    """
    用 GatekeeperAgent 做 LLM 知識萃取。
    
    2026.03.10 George: v2.4 重構 skills_context / merge_hint / existing_skills_catalog
    - 新增 existing_skills_catalog：無論 skills_referenced 有無值，都把所有現有
      skills 的 frontmatter（name + description）餵進 prompt，讓 LLM 在產出
      skill_name 時能直接對齊到已存在的 skill
    - skills_context（v2.1 原有）：僅在 skills_referenced 有值時啟用，
      告知 LLM 哪些 skills 已被參考過，聚焦萃取尚未涵蓋的新知識
    - merge_hint（v2.1 原有）：進一步強調優先使用已參考 skill 的名稱
    - 三者共用同一次 LLM call（都在 KNOWLEDGE_EXTRACTION_PROMPT 內），零額外成本
    
    2026.02.28 George: v2.1 新增 skills_context 和 merge_hint
    """
    if not payload.error_messages:
        return None

    errors_text = "\n---\n".join(
        f"### 嘗試 #{i+1}\n{err[:500]}"
        for i, err in enumerate(payload.error_messages)
    )

    # ══════════════════════════════════════════════════════════════
    # 2026.03.10 v2.4: 組裝三段 LLM context（全部塞進同一個 prompt）
    # ══════════════════════════════════════════════════════════════
    
    # ── (A) existing_skills_catalog: 所有現有 skills 的 frontmatter 摘要 ──
    # 無論 skills_referenced 有無值都帶上，讓 LLM 能對齊 skill_name
    # 避免 LLM 每次自創新名導致同概念的 skill 被重複建立
    existing_catalog_raw = _collect_existing_skills_summary()
    if existing_catalog_raw:
        existing_skills_catalog = (
            f"## 系統中已有的 Skills\n"
            f"以下是目前已存在的 skills 清單（name + description），\n"
            f"如果萃取出的知識與其中某個 skill 相關，請直接使用該 skill 的 name。\n\n"
            f"{existing_catalog_raw}"
        )
    else:
        existing_skills_catalog = ""
    
    # ── (B) skills_context: CodingAgent 本次實際參考了哪些 skills ──
    # 僅在 skills_referenced 有值時啟用（v2.1 原有邏輯）
    skills_context = ""
    merge_hint = ""
    if payload.skills_referenced:
        skills_list = ", ".join(payload.skills_referenced)
        skills_context = (
            f"## CodingAgent 已參考的 Skills\n"
            f"本次 CodingAgent 在產生程式碼時已參考了以下 skills: {skills_list}\n"
            f"這表示上述 skills 的知識已被考慮過，但仍然遇到了新的錯誤。\n"
            f"請聚焦於萃取這些 skills **尚未涵蓋** 的新知識。"
        )
        # ── (C) merge_hint: 強調優先使用已參考 skill 的名稱 ──
        merge_hint = (
            f"重要：因為 CodingAgent 已參考 {skills_list}，"
            f"請優先使用這些已有 skill 的名稱作為 skill_name，"
            f"以便知識合併回現有 skill 而非新建。"
        )

    prompt = KNOWLEDGE_EXTRACTION_PROMPT.format(
        user_request=payload.user_request,
        final_code=payload.final_code[:4000],
        errors_text=errors_text,
        skills_context=skills_context,
        existing_skills_catalog=existing_skills_catalog,
        merge_hint=merge_hint,
    )

    # ══════════════════════════════════════════════════════════════
    # 2026.03.10 v2.4.1: Prompt 長度安全檢查
    # 問題：v2.4 加入 existing_skills_catalog 後，prompt 可能超過
    # Gatekeeper 模型的 input token limit，導致 API 回 error
    # 策略：漸進式裁剪 — 先砍 catalog，再砍 final_code，確保 prompt
    # 長度不超過安全上限
    # ══════════════════════════════════════════════════════════════
    # 粗估：1 token ≈ 2-3 中文字 ≈ 4 英文字元
    # 保守上限 20000 字元 ≈ ~6666-10000 tokens，留足 output 空間
    PROMPT_MAX_CHARS = 20000
    
    if len(prompt) > PROMPT_MAX_CHARS:
        logger.warning(
            f"[Gatekeeper] Prompt 過長 ({len(prompt)} chars > {PROMPT_MAX_CHARS})，"
            f"開始裁剪"
        )
        
        # 第一步：移除 existing_skills_catalog（對萃取品質影響最小）
        if existing_skills_catalog:
            prompt = KNOWLEDGE_EXTRACTION_PROMPT.format(
                user_request=payload.user_request,
                final_code=payload.final_code[:4000],
                errors_text=errors_text,
                skills_context=skills_context,
                existing_skills_catalog="",  # 移除 catalog
                merge_hint=merge_hint,
            )
            logger.info(
                f"[Gatekeeper] 裁剪 Step 1: 移除 existing_skills_catalog "
                f"→ {len(prompt)} chars"
            )
        
        # 第二步：如果還是太長，縮短 final_code
        if len(prompt) > PROMPT_MAX_CHARS:
            prompt = KNOWLEDGE_EXTRACTION_PROMPT.format(
                user_request=payload.user_request,
                final_code=payload.final_code[:2000],  # 從 4000 縮到 2000
                errors_text=errors_text,
                skills_context=skills_context,
                existing_skills_catalog="",
                merge_hint=merge_hint,
            )
            logger.info(
                f"[Gatekeeper] 裁剪 Step 2: final_code 縮短為 2000 chars "
                f"→ {len(prompt)} chars"
            )
    
    logger.info(f"[Gatekeeper] LLM prompt 長度: {len(prompt)} chars")

    try:
        # 使用 agent_framework Agent 呼叫方式
        from agent_framework import Message
        messages = [Message("user", [prompt])]

        # 2026.03.10 v2.4.1: 移除 asyncio.wait_for，改回同步 streaming
        # 原因：Gatekeeper 已與主流程切割（fire-and-forget），不需要 timeout，
        # 寧可跑久一點拿到完整結果和 log，也不要 timeout 後什麼都看不到
        # 改為在每個關鍵步驟加 log，方便追蹤卡在哪一步
        
        logger.info("[Gatekeeper] LLM 開始呼叫 gatekeeper_agent.run()...")
        response_text = ""
        chunk_count = 0
        stream = gatekeeper_agent.run(messages, stream=True)
        
        logger.info("[Gatekeeper] LLM stream 已建立，開始接收 chunks...")
        async for update in stream:
            text = update.text or ""
            if text:
                response_text += text
                chunk_count += 1
        
        logger.info(
            f"[Gatekeeper] LLM streaming 完成: "
            f"收到 {chunk_count} 個 chunks, "
            f"回應總長 {len(response_text)} chars"
        )

        # 解析 JSON
        result = _parse_json_response(response_text)
        if not result:
            logger.warning("[Gatekeeper] LLM 回應無法解析為 JSON")
            return None

        if result.get("skip"):
            logger.info(f"[Gatekeeper] LLM 判斷不需萃取: {result.get('reason')}")
            return None

        return result

    except Exception as e:
        logger.error(f"[Gatekeeper] LLM 知識萃取失敗: {type(e).__name__}: {e}")
        return None


def extract_knowledge_by_rules(payload: GatekeeperPayload) -> Optional[Dict]:
    """
    純規則的知識萃取 fallback（當 LLM 不可用時）。
    從錯誤訊息中提取常見 pattern。
    """
    if not payload.error_messages:
        return None

    known_issues = []
    for i, error_text in enumerate(payload.error_messages):
        # 提取 Python 錯誤類型
        error_match = re.search(
            r'(ModuleNotFoundError|ImportError|FileNotFoundError|'
            r'PermissionError|ConnectionError|TimeoutError|'
            r'TypeError|ValueError|KeyError|AttributeError'
            r'):\s*(.+?)(?:\n|$)', error_text
        )
        if error_match:
            error_type = error_match.group(1)
            error_detail = error_match.group(2).strip()[:200]
            known_issues.append({
                "title": f"{error_type}: {error_detail[:60]}",
                "error_pattern": f"{error_type}: {error_detail}",
                "root_cause": "從執行歷程自動萃取（需人工確認）",
                "solution": "參考最終成功的程式碼版本",
                "applicable_when": f"使用類似技術時遇到 {error_type}",
            })

    if not known_issues:
        return None

    # 2026.02.28 George: v2.1 如果有參考 skills，優先用已有 skill name
    if payload.skills_referenced:
        safe_name = _sanitize_skill_name(payload.skills_referenced[0])
    else:
        safe_name = _generate_skill_name_from_request(payload.user_request)

    return {
        "skill_name": f"{safe_name}-tips" if not payload.skills_referenced else safe_name,
        "skill_description": f"使用 {safe_name} 相關技術時的已知問題與解法",
        "known_issues": known_issues,
        "workflow_patterns": [],  # 2026.03.27 v2.5: 規則層無法萃取 workflow pattern，留空
        "useful_packages": _extract_package_names(payload.final_code),
        "tags": _extract_tags(payload),
    }


# ============================================================================
# PHASE 3: DEDUPLICATION（簡化查重 — P0）
# ============================================================================

def check_duplicate(
    skill_name: str,
    skill_description: str,
    skills_dir: str = None,
) -> Optional[str]:
    """
    簡化查重：掃描本地 skills/ 目錄，比對 name 和 description 關鍵字。
    
    Returns:
        重複的 skill 目錄路徑（如果找到），否則 None
    """
    skills_dir = skills_dir or SKILLS_DIR
    if not os.path.exists(skills_dir):
        return None

    new_keywords = set(_tokenize(skill_name) + _tokenize(skill_description))

    for item in os.listdir(skills_dir):
        skill_md_path = os.path.join(skills_dir, item, "SKILL.md")
        if not os.path.isfile(skill_md_path):
            continue

        try:
            with open(skill_md_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 2026.03.05 v2.3: 三元組回傳，此處不需要 metadata
            existing_name, existing_desc, _ = _parse_skill_frontmatter(content)
            existing_keywords = set(
                _tokenize(existing_name) + _tokenize(existing_desc)
            )

            # 計算交集
            overlap = new_keywords & existing_keywords
            if len(overlap) >= DEDUP_SIMILARITY_KEYWORDS:
                return os.path.join(skills_dir, item)

        except Exception:
            continue

    return None


def _find_skill_dir_by_name(skill_name: str, skills_dir: str = None) -> Optional[str]:
    """
    2026.02.28 George: v2.1
    根據 skill name 精確查找 skill 目錄。
    用於 skills_referenced 的合併場景。
    
    Returns:
        skill 目錄路徑（如果找到），否則 None
    """
    skills_dir = skills_dir or SKILLS_DIR
    if not os.path.exists(skills_dir):
        return None
    
    # 先精確比對目錄名
    exact_path = os.path.join(skills_dir, skill_name)
    if os.path.isdir(exact_path) and os.path.isfile(os.path.join(exact_path, "SKILL.md")):
        return exact_path
    
    # 再模糊比對 SKILL.md 裡的 name 欄位
    for item in os.listdir(skills_dir):
        skill_md_path = os.path.join(skills_dir, item, "SKILL.md")
        if not os.path.isfile(skill_md_path):
            continue
        try:
            with open(skill_md_path, "r", encoding="utf-8") as f:
                content = f.read()
            # 2026.03.05 v2.3: 三元組回傳，此處不需要 desc 和 metadata
            existing_name, _, _ = _parse_skill_frontmatter(content)
            if existing_name and _sanitize_skill_name(existing_name) == _sanitize_skill_name(skill_name):
                return os.path.join(skills_dir, item)
        except Exception:
            continue

    return None


# 2026.08.24 George : v2.8 — merge 基準以 Blob 為真實來源
#
# 本地 SKILLS_DIR 只在啟動時同步一次,而 Mode B 每一輪都從 Blob materialize。
# 只要別台 replica 的 Gatekeeper 寫過、或 DBA 人工發布後沒重啟,本地就是舊的:
# 輕則版本號倒退、dedup 失準,重則目標根本不在本地而被模糊比對亂點鴛鴦。
def _hydrate_skill_md(skills_dir: str, skill_name: str, content: str) -> Optional[str]:
    """把 Blob 拉回來的 SKILL.md 寫進本地 skill 目錄,回傳該目錄路徑。"""
    target_dir = os.path.join(skills_dir, skill_name)
    try:
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(content)
        return target_dir
    except Exception as e:
        logger.warning(f"[Gatekeeper] 寫入本地 merge 基準失敗: {target_dir} — {e}")
        return None


async def _resolve_and_refresh_skill_dir(
    skill_name: str,
    skills_dir: str = None,
) -> tuple[Optional[str], Optional[str]]:
    """
    2026.08.24 George: v2.8 新增

    定位要 merge 的 skill 目錄,並先把 Blob 上的 SKILL.md 拉回來當基準。

    Returns:
        (skill 目錄, SKILL.md 的 Blob ETag)。Blob 拿不到時退回純本地查找,
        ETag 為 None、上傳時走無條件覆蓋(維持舊行為,不因為 Blob 不可用
        就連寫回一起停掉)。兩邊都沒有則回 (None, None)。
    """
    skills_dir = skills_dir or SKILLS_DIR

    try:
        from skills_sync import fetch_skill_md_from_blob
        fetched = await fetch_skill_md_from_blob(skill_name)
    except Exception as e:
        logger.warning(f"[Gatekeeper] Blob 基準取用失敗，退回本地: {skill_name} — {e}")
        fetched = None

    if fetched:
        content, etag = fetched
        hydrated = _hydrate_skill_md(skills_dir, skill_name, content)
        if hydrated:
            return hydrated, etag

    return _find_skill_dir_by_name(skill_name, skills_dir), None


# 2026.08.21 George : Skill Registry 分階 —— 識別「情境層 / 編排層」skill
#
# 不用 _parse_skill_frontmatter() 是因為它是手寫的逐行 parser,遇到
# `children:` 下接 block-style list 時只會拿到空字串,偵測會静静失效。
# 這裡的判斷結果會決定要不要寫回 skill,偵測失效的代價太大。
def _is_orchestration_skill(skill_dir: str) -> bool:
    """該 skill 的 frontmatter 是否宣告了 metadata.children。"""
    skill_md_path = os.path.join(skill_dir, "SKILL.md")
    try:
        with open(skill_md_path, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.startswith("---"):
            return False
        end = content.find("---", 3)
        if end == -1:
            return False
        parsed = yaml.safe_load(content[3:end])
        if not isinstance(parsed, dict):
            return False
        metadata = parsed.get("metadata")
        return isinstance(metadata, dict) and bool(metadata.get("children"))
    except Exception as e:
        # 偵測不確定時偷向「不是編排層」—— 保持現行合併行為,不因為
        # 一個壞掉的 YAML 就讓整個 Gatekeeper 學不到東西。
        logger.warning(
            f"[Gatekeeper] 無法判定 {os.path.basename(skill_dir)} 是否為編排層：{e}"
        )
        return False


# ============================================================================
# PHASE 3.5: RELEVANCE CHECK（v2.2 新增）
# 2026.03.04 George: 驗證萃取出的知識是否與目標 skill 真正相關
# 解決問題：CodingAgent 因關鍵字太泛（如 "azure"）誤匹配 skill，
# 導致不相關知識被合併回錯誤的 skill（例如 DALL-E 知識進了 diagrams skill）
# ============================================================================

def check_relevance_for_merge(
    knowledge: Dict,
    target_skill_dir: str,
) -> float:
    """
    驗證萃取出的新知識是否與目標 skill 真正相關。
    
    2026.03.10 George: v2.4.1 重構 — 回傳 relevance score 而非 bool
    - 問題：原本回傳 bool，在 skills_referenced 有多個 skill 時，迴圈用
      break 取第一個 PASS 的 skill，而非最相關的。例如 diagrams 相關的知識
      被合併到列表中第一個碰巧 PASS 的 azure-ad skill
    - 修正：回傳 0.0-1.0 的 relevance score，讓呼叫端能比較所有候選，
      選擇分數最高的 skill 合併
    - 向後相容：score > 0 等同原本的 True，score == 0 等同 False
    
    評分規則：
    - 維度 1 (Tags meaningful overlap):    0.4 分
    - 維度 2 (Description keyword overlap): 0.3 分
    - 維度 3 (Package overlap):            0.3 分
    - 各維度分數加總，最高 1.0
    
    Returns:
        0.0 = 不相關（原本的 False）
        >0.0 = 相關（原本的 True），數值越高越相關
    """
    skill_md_path = os.path.join(target_skill_dir, "SKILL.md")
    target_name = os.path.basename(target_skill_dir)

    # PHASE 3 會對每個候選 skill 各跑一次，中間過程一律走 debug，只留最後 PASS/FAIL 一行 info
    logger.debug(f"[Relevance] Checking target skill: {target_name}")

    if not os.path.isfile(skill_md_path):
        logger.warning(f"[Relevance] Target SKILL.md not found: {skill_md_path}")
        return 0.0

    try:
        with open(skill_md_path, "r", encoding="utf-8") as f:
            existing_content_raw = f.read()
            existing_content = existing_content_raw.lower()
    except Exception as e:
        logger.error(f"[Relevance] Failed to read existing skill: {e}")
        return 0.0

    existing_name, existing_desc, _ = _parse_skill_frontmatter(existing_content_raw)
    
    existing_desc_tokens = set(_tokenize(existing_desc))
    existing_content_tokens = set(_tokenize(existing_content))
    
    logger.debug(f"[Relevance] Target tokens: Desc={existing_desc_tokens}, Content_count={len(existing_content_tokens)}")

    # ── 累積 score，三個維度各自評分 ──
    score = 0.0

    # ── 維度 1: Tags 重疊 (max 0.4) ──
    new_tags = set(t.lower() for t in knowledge.get("tags", []))
    tag_overlap = new_tags & existing_content_tokens
    
    logger.debug(f"[Relevance] Checking Tags: New={new_tags}, Overlap={tag_overlap}")

    if len(tag_overlap) >= RELEVANCE_MIN_TAG_OVERLAP:
        generic_tags = {"azure", "python", "api", "web", "data", "cloud", "json", "csv", "debug", "ai", "issues", "error", "skill", "solution"}
        meaningful_overlap = tag_overlap - generic_tags
        if meaningful_overlap:
            score += 0.4
            logger.debug(f"[Relevance] Tags: +0.4 (meaningful overlap={meaningful_overlap})")
        else:
            logger.debug(f"[Relevance] Tags: +0.0 (only generic: {tag_overlap})")
    
    # ── 維度 2: Description 關鍵字重疊 (max 0.3) ──
    new_desc = knowledge.get("skill_description", "")
    new_desc_tokens = set(_tokenize(new_desc))
    desc_overlap = new_desc_tokens & existing_desc_tokens
    
    generic_words = {
        "azure", "python", "using", "tips", "issues", "known", "related",
        "when", "with", "that", "this", "from", "about", "have", "error",
        "skill", "problem", "solution",
    }
    meaningful_desc_overlap = desc_overlap - generic_words
    
    logger.debug(f"[Relevance] Checking Desc Keywords: Overlap={meaningful_desc_overlap} (Required >= {RELEVANCE_MIN_KEYWORD_OVERLAP})")

    if len(meaningful_desc_overlap) >= RELEVANCE_MIN_KEYWORD_OVERLAP:
        score += 0.3
        logger.debug(f"[Relevance] Desc: +0.3 (overlap={meaningful_desc_overlap})")

    # ── 維度 3: Package 重疊 (max 0.3) ──
    new_packages = set(p.lower() for p in knowledge.get("useful_packages", []))
    if new_packages:
        pkg_overlap = new_packages & existing_content_tokens
        logger.debug(f"[Relevance] Checking Packages: New={new_packages}, Overlap={pkg_overlap}")
        if pkg_overlap:
            score += 0.3
            logger.debug(f"[Relevance] Pkgs: +0.3 (overlap={pkg_overlap})")
    
    # ── 最終結果 ──
    if score > 0:
        logger.info(f"[Relevance] ✅ PASS: '{target_name}' score={score:.1f}")
    else:
        logger.info(
            f"[Relevance] ❌ FAIL: '{knowledge.get('skill_name')}' is NOT relevant to '{target_name}'. "
            f"Dimensions: Tags({len(tag_overlap)}), DescOverlap({len(meaningful_desc_overlap)}), "
            f"Pkgs({new_packages if not new_packages else pkg_overlap})"
        )
    
    return score


def write_knowledge_skill(
    knowledge: Dict,
    existing_skill_dir: Optional[str] = None,
    skills_dir: str = None,
) -> Optional[str]:
    """
    將知識萃取結果寫入本地 skills/ 目錄。
    
    符合 Agent Skills 規範（供 SkillsProvider discover）：
    - skills/{skill-name}/SKILL.md（含 YAML frontmatter）
    - 目錄名 = YAML name 欄位
    
    如果 existing_skill_dir 非 None，代表找到重複的 skill，
    就 append 到現有 SKILL.md 而非新建。
    
    Returns:
        寫入的 skill 目錄路徑
    """
    skills_dir = skills_dir or SKILLS_DIR
    os.makedirs(skills_dir, exist_ok=True)

    skill_name = knowledge.get("skill_name", "unknown-skill")
    # 確保 name 符合 Agent Skills 規範：小寫、連字號、無連續連字號
    skill_name = _sanitize_skill_name(skill_name)

    if existing_skill_dir:
        # ── 合併到現有 skill ──
        return _merge_into_existing_skill(existing_skill_dir, knowledge)
    else:
        # ── 新建 skill ──
        return _create_new_skill(skills_dir, skill_name, knowledge)


def _create_new_skill(skills_dir: str, skill_name: str, knowledge: Dict) -> str:
    """新建一個 knowledge 型 skill 目錄"""
    skill_dir = os.path.join(skills_dir, skill_name)
    os.makedirs(skill_dir, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    description = knowledge.get("skill_description", "Auto-generated knowledge skill")
    tags = knowledge.get("tags", [])
    packages = knowledge.get("useful_packages", [])
    known_issues = knowledge.get("known_issues", [])

    # ── 產生 SKILL.md ──
    md_lines = [
        "---",
        f"name: {skill_name}",
        f'description: "{_escape_yaml_string(description)}"',
        "metadata:",
        f"  author: gatekeeper-auto",
        f"  version: \"1.0\"",
        f"  created_at: \"{now}\"",
        f"  skill_type: knowledge",
        "---",
        "",
        f"# {skill_name}",
        "",
        description,
        "",
    ]

    # Tags
    if tags:
        md_lines.append(f"**相關技術**: {', '.join(tags)}")
        md_lines.append("")

    # Packages
    if packages:
        md_lines.append(f"**相關套件**: {', '.join(packages)}")
        md_lines.append("")

    # Known Issues
    if known_issues:
        md_lines.append("## 已知問題與 Workaround")
        md_lines.append("")
        for issue in known_issues:
            md_lines.append(f"### {issue.get('title', 'Unknown Issue')}")
            md_lines.append("")
            if issue.get("error_pattern"):
                md_lines.append(f"**錯誤訊息**: `{issue['error_pattern'][:200]}`")
                md_lines.append("")
            if issue.get("root_cause"):
                md_lines.append(f"**根因**: {issue['root_cause']}")
                md_lines.append("")
            if issue.get("solution"):
                md_lines.append(f"**解法**: {issue['solution']}")
                md_lines.append("")
            if issue.get("applicable_when"):
                md_lines.append(f"**適用情境**: {issue['applicable_when']}")
                md_lines.append("")

    # 2026.03.27 v2.5: Workflow Patterns
    workflow_patterns = knowledge.get("workflow_patterns", [])
    if workflow_patterns:
        md_lines.append("## 建議工作流程")
        md_lines.append("")
        for wp in workflow_patterns:
            md_lines.append(f"### {wp.get('title', 'Workflow Pattern')}")
            md_lines.append("")
            if wp.get("trigger"):
                md_lines.append(f"**適用時機**: {wp['trigger']}")
                md_lines.append("")
            if wp.get("steps"):
                md_lines.append(f"**步驟**:")
                md_lines.append("")
                md_lines.append(wp['steps'])
                md_lines.append("")
            if wp.get("rationale"):
                md_lines.append(f"**為什麼需要**: {wp['rationale']}")
                md_lines.append("")

    # 安全護欄
    md_lines.extend([
        "## ⚠️ 注意事項",
        "",
        "- 此 Skill 由 Gatekeeper 自動產生，內容來自實際執行歷程",
        "- 已知問題的解法可能隨套件版本更新而失效，請注意版本相容性",
        f"- 最後更新: {now}",
        "",
    ])

    skill_md_content = "\n".join(md_lines)
    skill_md_path = os.path.join(skill_dir, "SKILL.md")

    with open(skill_md_path, "w", encoding="utf-8") as f:
        f.write(skill_md_content)

    logger.info(f"[Gatekeeper] 新建 Skill: {skill_dir}")
    return skill_dir


def _merge_into_existing_skill(skill_dir: str, knowledge: Dict) -> str:
    """
    將新知識 append 到現有 skill 的 SKILL.md
    
    2026.03.05 George: v2.3 通用化格式支援
    - 新增 _detect_skill_origin() 分流：
      - gatekeeper-auto: 走原有固定 marker 邏輯（**相關套件**、## 已知問題、## ⚠️ 注意事項）
      - manual: 走新的 「## Gatekeeper Addendum」獨立 section 策略，
        不破壞人工編排的結構
    - 版本遞增改用 _increment_version()，同時相容 semver 和簡化版格式
    
    2026.03.05 George: v2.2 修正 useful_packages 資訊斷層
    - merge 模式下也會更新「相關套件」行，將新 packages union 進現有清單
    - 解決 check_relevance_for_merge 維度 3 (Package 重疊) 日漸失準的問題
    """
    skill_md_path = os.path.join(skill_dir, "SKILL.md")

    if not os.path.exists(skill_md_path):
        logger.warning(f"[Gatekeeper] 找不到 {skill_md_path}，改為新建")
        skill_name = os.path.basename(skill_dir)
        parent_dir = os.path.dirname(skill_dir)
        return _create_new_skill(parent_dir, skill_name, knowledge)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    known_issues = knowledge.get("known_issues", [])

    if not known_issues:
        return skill_dir

    # 讀取現有內容
    with open(skill_md_path, "r", encoding="utf-8") as f:
        existing_content = f.read()

    # ═══════════════════════════════════════════════════════════════
    # 2026.03.05 v2.3: 偵測 skill 來源，決定 merge 策略
    # ═══════════════════════════════════════════════════════════════
    _, _, metadata = _parse_skill_frontmatter(existing_content)
    origin = _detect_skill_origin(existing_content, metadata)
    logger.info(f"[Gatekeeper] Merge 策略: origin={origin}, target={os.path.basename(skill_dir)}")

    if origin == "gatekeeper-auto":
        # ── 路線 A: Gatekeeper 自產 skill → 走原有固定 marker 邏輯 ──
        updated_content = _merge_gatekeeper_auto(existing_content, knowledge, now)
    else:
        # ── 路線 B: 人工撰寫 skill → 走 Addendum section 邏輯 ──
        updated_content = _merge_manual_skill(existing_content, knowledge, now)

    # ═══════════════════════════════════════════════════════════════
    # 2026.03.10 v2.4: 若 merge 函式回傳內容完全未變（所有 issues 均重複），
    # 跳過版本遞增和檔案寫入，避免無意義的版本號膨脹
    # ═══════════════════════════════════════════════════════════════
    if updated_content == existing_content:
        logger.info(f"[Gatekeeper] 內容未變更，跳過寫入和版本遞增: {skill_dir}")
        return skill_dir

    # ═══════════════════════════════════════════════════════════════
    # 2026.03.05 v2.3: 通用版本遞增（相容 semver 和簡化版）
    # 取代原本只認 version: "1.0" 格式的 regex
    # ═══════════════════════════════════════════════════════════════
    version_match = re.search(r'(version:\s*)(.+)', updated_content)
    if version_match:
        prefix = version_match.group(1)         # "version: " 或 "version:  "
        old_ver_str = version_match.group(2)    # "1.0" 或 1.0.0 等
        new_ver_str = _increment_version(old_ver_str)
        if new_ver_str != old_ver_str:
            updated_content = updated_content.replace(
                version_match.group(0),
                f"{prefix}{new_ver_str}",
                1  # 只替換第一個匹配（frontmatter 中的）
            )
            logger.info(f"[Gatekeeper] 版本遞增: {old_ver_str.strip()} → {new_ver_str.strip()}")

    with open(skill_md_path, "w", encoding="utf-8") as f:
        f.write(updated_content)

    logger.info(f"[Gatekeeper] 更新 Skill ({origin}): {skill_dir} (+{len(known_issues)} issues)")
    return skill_dir


def _merge_gatekeeper_auto(existing_content: str, knowledge: Dict, now: str) -> str:
    """
    路線 A: 合併到 Gatekeeper 自動產生的 SKILL.md
    
    2026.03.10 George: v2.4 加入 issue-level dedup
    - 在 append 前呼叫 _dedup_known_issues() 過濾已存在的 issues
    - 若去重後無新 issue，直接 return 不做任何修改（也不遞增版本號）
    
    2026.03.05 George: v2.3 從 _merge_into_existing_skill 抽出
    此函式保留 v2.2 原有邏輯不變：
    - 找 **相關套件** 行做 package union
    - 找 ## ⚠️ 注意事項 做 known issues 插入
    - 找不到 marker 時用 fallback
    
    前提：target SKILL.md 必須包含 Gatekeeper 自產的固定結構
    """
    known_issues = knowledge.get("known_issues", [])
    # 2026.03.27 v2.5: 取得 workflow_patterns
    workflow_patterns = knowledge.get("workflow_patterns", [])
    
    # ── 2026.03.10 v2.4: Issue-level 去重 ──
    # 在做任何 append 之前，先過濾掉已存在的 issues
    known_issues = _dedup_known_issues(known_issues, existing_content)
    
    if not known_issues and not workflow_patterns:
        # 所有 issues 都是重複的，也沒有新 workflow pattern → 不修改檔案
        logger.info("[Gatekeeper] 所有 issues 均為重複且無新 workflow pattern，跳過 merge (auto)")
        return existing_content
    
    # ── 合併 useful_packages 到現有「相關套件」行 (v2.2 邏輯) ──
    new_packages = knowledge.get("useful_packages", [])
    if new_packages:
        pkg_match = re.search(r'\*\*相關套件\*\*:\s*(.+)', existing_content)
        if pkg_match:
            existing_pkgs = set(
                p.strip() for p in pkg_match.group(1).split(",") if p.strip()
            )
            merged_pkgs = sorted(existing_pkgs | set(new_packages))
            existing_content = existing_content.replace(
                pkg_match.group(0),
                f"**相關套件**: {', '.join(merged_pkgs)}"
            )
            logger.info(
                f"[Gatekeeper] 合併套件: existing={existing_pkgs}, "
                f"new={set(new_packages)}, merged={merged_pkgs}"
            )
        else:
            insert_before = "## 已知問題與 Workaround"
            if insert_before in existing_content:
                existing_content = existing_content.replace(
                    insert_before,
                    f"**相關套件**: {', '.join(sorted(new_packages))}\n\n{insert_before}"
                )
                logger.info(f"[Gatekeeper] 新增套件行: {new_packages}")

    # ── 產生新的 known issues 段落（只包含去重後的 issues）──
    new_section = ""
    if known_issues:
        new_section += _format_known_issues_section(known_issues, now)

    # ── 2026.03.27 v2.5: 產生 workflow patterns 段落 ──
    if workflow_patterns:
        wp_section = _format_workflow_patterns_section(workflow_patterns, now)
        if wp_section:
            new_section += wp_section

    if not new_section:
        return existing_content

    # ── 插入到 ## ⚠️ 注意事項 前 ──
    # 2026.03.27 v2.5: 如果 ## 建議工作流程 已存在，workflow patterns 插入到其尾端
    # 否則和 known_issues 一起插入到 ## ⚠️ 注意事項 前
    insert_marker = "## ⚠️ 注意事項"
    if insert_marker in existing_content:
        existing_content = existing_content.replace(
            insert_marker,
            f"{new_section}\n{insert_marker}"
        )
    else:
        existing_content += new_section

    return existing_content


def _merge_manual_skill(existing_content: str, knowledge: Dict, now: str) -> str:
    """
    路線 B: 合併到人工撰寫的 SKILL.md
    
    2026.03.10 George: v2.4 加入 issue-level dedup
    - 在 append 前呼叫 _dedup_known_issues() 過濾已存在的 issues
    - 若去重後無新 issue，直接 return 不做任何修改
    
    2026.03.05 George: v2.3 新增
    
    策略：使用獨立的 「## Gatekeeper Addendum」section 進行追加
    - 不修改人工編排的任何既有內容（heading、段落、程式碼區塊等）
    - 所有 Gatekeeper 自動追加的知識都集中在 Addendum section
    - 如果 Addendum section 已存在（之前 merge 過），在其尾端追加
    - 如果不存在，在檔案尾端新建 section
    
    好處：
    - 人工撰寫者可以隨時刪除或編輯 Addendum section 而不影響原始內容
    - 避免 Gatekeeper 把知識塞到不適當的位置
    - 結構清晰，一眼可辨哪些是自動產生的
    """
    known_issues = knowledge.get("known_issues", [])
    new_packages = knowledge.get("useful_packages", [])
    # 2026.03.27 v2.5: 取得 workflow_patterns
    workflow_patterns = knowledge.get("workflow_patterns", [])
    
    # ── 2026.03.10 v2.4: Issue-level 去重 ──
    # manual skill 也需要去重，因為 Addendum section 會不斷累積
    known_issues = _dedup_known_issues(known_issues, existing_content)
    
    if not known_issues and not workflow_patterns:
        # 所有 issues 都是重複的，也沒有新 workflow pattern → 不修改檔案
        logger.info("[Gatekeeper] 所有 issues 均為重複且無新 workflow pattern，跳過 merge (manual)")
        return existing_content
    
    # ── 產生 Addendum 內容 ──
    addendum_lines = [
        f"<!-- Gatekeeper 自動更新: {now} -->",
    ]
    
    # packages 資訊（寫在 addendum 內，不去動原文）
    if new_packages:
        addendum_lines.append(f"**相關套件**: {', '.join(sorted(new_packages))}")
        addendum_lines.append("")
    
    # known issues
    for issue in known_issues:
        addendum_lines.append(f"### {issue.get('title', 'Unknown Issue')}")
        addendum_lines.append("")
        if issue.get("error_pattern"):
            addendum_lines.append(f"**錯誤訊息**: `{issue['error_pattern'][:200]}`")
            addendum_lines.append("")
        if issue.get("root_cause"):
            addendum_lines.append(f"**根因**: {issue['root_cause']}")
            addendum_lines.append("")
        if issue.get("solution"):
            addendum_lines.append(f"**解法**: {issue['solution']}")
            addendum_lines.append("")
        if issue.get("applicable_when"):
            addendum_lines.append(f"**適用情境**: {issue['applicable_when']}")
            addendum_lines.append("")
    
    # 2026.03.27 v2.5: workflow patterns
    for wp in workflow_patterns:
        addendum_lines.append(f"### 🔄 {wp.get('title', 'Workflow Pattern')}")
        addendum_lines.append("")
        if wp.get("trigger"):
            addendum_lines.append(f"**適用時機**: {wp['trigger']}")
            addendum_lines.append("")
        if wp.get("steps"):
            addendum_lines.append(f"**步驟**:")
            addendum_lines.append("")
            addendum_lines.append(wp['steps'])
            addendum_lines.append("")
        if wp.get("rationale"):
            addendum_lines.append(f"**為什麼需要**: {wp['rationale']}")
            addendum_lines.append("")
    
    new_content = "\n".join(addendum_lines)
    
    # ── 定位 Addendum section ──
    addendum_heading = "## Gatekeeper Addendum"
    
    if addendum_heading in existing_content:
        # 已有 Addendum section → 在其尾端追加
        # 找到 Addendum 後的下一個 ## heading（如果有的話）作為邊界
        addendum_pos = existing_content.index(addendum_heading)
        after_heading = existing_content[addendum_pos + len(addendum_heading):]
        
        # 找下一個同級 heading（## 開頭，但不是 ###）
        next_h2_match = re.search(r'\n(?=## [^#])', after_heading)
        
        if next_h2_match:
            # 在下一個 ## heading 前插入
            insert_pos = addendum_pos + len(addendum_heading) + next_h2_match.start()
            existing_content = (
                existing_content[:insert_pos]
                + f"\n{new_content}\n"
                + existing_content[insert_pos:]
            )
        else:
            # 沒有後續 ## heading → 直接 append 到檔案尾端
            existing_content = existing_content.rstrip() + f"\n\n{new_content}\n"
        
        logger.info(f"[Gatekeeper] 追加到既有 Addendum section")
    else:
        # 新建 Addendum section → append 到檔案尾端
        existing_content = (
            existing_content.rstrip()
            + f"\n\n{addendum_heading}\n\n"
            + f"> 以下內容由 Gatekeeper 自動萃取，記錄實際執行中遇到的問題與解法。\n\n"
            + new_content
            + "\n"
        )
        logger.info(f"[Gatekeeper] 新建 Addendum section")
    
    return existing_content


def _format_known_issues_section(known_issues: List[Dict], now: str) -> str:
    """
    格式化 known issues 為 Markdown 段落。
    
    2026.03.05 George: v2.3 從 _merge_gatekeeper_auto 和 _merge_manual_skill 
    共用的格式化邏輯抽出為獨立函式，減少重複。
    但因兩條路線的結構略有不同（auto 用 HTML comment 開頭，manual 用不同的包裝），
    目前此函式只被 _merge_gatekeeper_auto 呼叫。
    """
    lines = [
        "",
        f"<!-- Gatekeeper 自動更新: {now} -->",
    ]
    for issue in known_issues:
        lines.append(f"### {issue.get('title', 'Unknown Issue')}")
        lines.append("")
        if issue.get("error_pattern"):
            lines.append(f"**錯誤訊息**: `{issue['error_pattern'][:200]}`")
            lines.append("")
        if issue.get("root_cause"):
            lines.append(f"**根因**: {issue['root_cause']}")
            lines.append("")
        if issue.get("solution"):
            lines.append(f"**解法**: {issue['solution']}")
            lines.append("")
        if issue.get("applicable_when"):
            lines.append(f"**適用情境**: {issue['applicable_when']}")
            lines.append("")
    
    return "\n".join(lines)


# 2026.03.27 George: v2.5 新增 ─ Workflow Patterns 渲染
# ============================================================================
# 用途：將 workflow_patterns 列表渲染為 Markdown 段落
# 供 _merge_gatekeeper_auto 和 _merge_manual_skill 共用
# ============================================================================

def _format_workflow_patterns_section(workflow_patterns: List[Dict], now: str) -> str:
    """
    格式化 workflow patterns 為 Markdown 段落。
    
    2026.03.27 George: v2.5 新增
    """
    if not workflow_patterns:
        return ""
    
    lines = [
        "",
        f"<!-- Gatekeeper Workflow Pattern 自動更新: {now} -->",
    ]
    for wp in workflow_patterns:
        lines.append(f"### {wp.get('title', 'Workflow Pattern')}")
        lines.append("")
        if wp.get("trigger"):
            lines.append(f"**適用時機**: {wp['trigger']}")
            lines.append("")
        if wp.get("steps"):
            lines.append(f"**步驟**:")
            lines.append("")
            lines.append(wp['steps'])
            lines.append("")
        if wp.get("rationale"):
            lines.append(f"**為什麼需要**: {wp['rationale']}")
            lines.append("")
    
    return "\n".join(lines)


# ============================================================================
# PHASE 3.6: ISSUE-LEVEL DEDUP（v2.4 新增）
# 2026.03.10 George: 解決 SKILL.md 無限長大問題
# 根因：_merge_gatekeeper_auto / _merge_manual_skill 在 append known_issues 時
# 完全沒有比對新 issue 是否已存在，導致同一個錯誤（如 ModuleNotFoundError）
# 每次 Gatekeeper 觸發都重複寫入。
#
# 設計決策：不用 embedding/similarity，因為 error_pattern 已高度結構化
# （Python ErrorType: message），用 normalized key 比對即可精準去重，
# 且零外部依賴、毫秒級完成。
# ============================================================================

def _normalize_error_key(error_pattern: str) -> str:
    """
    2026.03.10 George: v2.4 新增
    
    從 error_pattern 提取可比對的 normalized key。
    將不同措辭但本質相同的錯誤訊息映射到同一個 key，用於跨次 merge 去重。
    
    正規化策略：
    1. 嘗試解析 "ErrorType: detail" 格式（Python 標準錯誤訊息）
    2. 從 detail 中提取引號內的關鍵字（module name、class name 等）
       → 這些是錯誤的「身份標識」，不會因 LLM 措辭不同而改變
    3. 若沒有引號內容，取前幾個有意義的 token 作為 fallback
    4. 若完全無法解析，用 MD5 hash 作為 last resort
    
    範例：
    - "ModuleNotFoundError: No module named 'diagrams'"
      → "modulenotfounderror:diagrams"
    - "ImportError: cannot import name 'DataFactory' from 'diagrams.azure.integration'"
      → "importerror:datafactory:diagrams.azure.integration"
    - "ImportError: cannot import name 'SQLManagedInstance' from 'diagrams.azure.database'"
      → "importerror:sqlmanagedinstance:diagrams.azure.database"
    - "TypeError: unsupported operand"
      → "typeerror:unsupported:operand"
    - "Graphviz binary 'dot' not found"
      → (無 ErrorType) fallback to MD5 hash
    
    Returns:
        正規化後的 key 字串（全小寫），用於集合比對
    """
    if not error_pattern:
        return ""
    
    text = error_pattern.strip().lower()
    
    # ── 策略 1: 解析 Python 標準錯誤格式 "ErrorType: detail" ──
    err_match = re.match(r'(\w+error|\w+exception):\s*(.*)', text)
    if err_match:
        err_type = err_match.group(1)       # e.g. "modulenotfounderror"
        detail = err_match.group(2)          # e.g. "no module named 'diagrams'"
        
        # 提取引號內的關鍵字（最可靠的身份標識）
        # 例如 module name、class name、package path 等
        quoted = re.findall(r"['\"]([^'\"]+)['\"]", detail)
        if quoted:
            # 用 ErrorType + 引號內容組成 key
            return f"{err_type}:{':'.join(quoted)}"
        
        # 沒有引號 → 取前幾個有意義的 token 作為 fallback
        # 過濾掉太短的 token（如 "no", "is" 等）
        tokens = [t for t in re.findall(r'[a-z0-9_.]+', detail) if len(t) >= 3][:3]
        if tokens:
            return f"{err_type}:{':'.join(tokens)}"
        
        # detail 太短或無有意義 token → 只用 ErrorType
        return err_type
    
    # ── 策略 2: 非標準格式 → 用 MD5 hash 作為 last resort ──
    # 例如 "Graphviz binary 'dot' not found" 沒有 ErrorType 前綴
    # 但仍可能有引號內容
    quoted_fallback = re.findall(r"['\"]([^'\"]+)['\"]", text)
    if quoted_fallback:
        # 用引號內容組 key（有一定辨識度）
        return f"_notype:{':'.join(quoted_fallback)}"
    
    # 完全無結構 → MD5 hash（至少保證完全相同的字串不會重複寫入）
    return hashlib.md5(text.encode()).hexdigest()[:16]


def _extract_existing_error_keys(existing_content: str) -> set:
    """
    2026.03.10 George: v2.4 新增
    
    從現有 SKILL.md 內容中，批次提取所有已記錄的 error_pattern normalized keys。
    
    掃描目標：所有 **錯誤訊息**: `...` 格式的行（Gatekeeper 標準輸出格式）。
    這是 _create_new_skill()、_merge_gatekeeper_auto()、_merge_manual_skill()
    產生 known_issues 時的統一格式，所以能可靠地回收所有已記錄的 error patterns。
    
    Returns:
        set of normalized key strings
    """
    keys = set()
    
    # 匹配 **錯誤訊息**: `<error_pattern>` 格式
    # 這是 Gatekeeper 所有路徑產生 issue 時的標準格式
    for m in re.finditer(r'\*\*錯誤訊息\*\*:\s*`([^`]+)`', existing_content):
        raw_pattern = m.group(1)
        key = _normalize_error_key(raw_pattern)
        if key:
            keys.add(key)
    
    return keys


def _dedup_known_issues(
    known_issues: List[Dict],
    existing_content: str,
) -> List[Dict]:
    """
    2026.03.10 George: v2.4 新增
    
    Issue-level 去重：過濾掉已存在於現有 SKILL.md 中的 known_issues。
    供 _merge_gatekeeper_auto() 和 _merge_manual_skill() 共用。
    
    比對策略（任一命中即視為重複，跳過該 issue）：
    1. error_pattern normalized key 完全匹配
       → 精確比對，能處理 LLM 不同措辭但本質相同的錯誤
       → 例："diagrams 套件未安裝" vs "找不到 diagrams 套件" 
         兩者的 error_pattern 都會包含 "ModuleNotFoundError: ... 'diagrams'"
         → normalized key 都是 "modulenotfounderror:diagrams" → 命中
    2. issue title 完全包含於現有內容（大小寫不敏感）
       → 防止 error_pattern 微調後繞過 key 比對的情況
       → 例：title 為 "執行環境未安裝 diagrams 套件導致無法匯入"，
         如果現有內容已有完全相同的 ### heading，就跳過
    
    同批次防重：
    - 維護 seen_keys set，防止同一次 LLM 萃取出多條相同 issue 都被寫入
    
    Args:
        known_issues: LLM 或 rule-based 萃取出的新 issues 列表
        existing_content: 現有 SKILL.md 的完整內容
    
    Returns:
        去重後的 issues 列表（可能為空）
    """
    if not known_issues:
        return []
    
    # ── Step 1: 從現有內容提取所有已記錄的 error keys ──
    existing_keys = _extract_existing_error_keys(existing_content)
    existing_lower = existing_content.lower()
    
    logger.info(
        f"[Dedup] 現有 SKILL.md 中找到 {len(existing_keys)} 個已記錄的 error keys: "
        f"{existing_keys}"
    )
    
    # ── Step 2: 逐條比對新 issues ──
    new_issues = []
    seen_keys = set()  # 同批次內防重（同一次 LLM 萃取可能產出重複 issue）
    
    for issue in known_issues:
        error_pattern = issue.get("error_pattern", "")
        title = issue.get("title", "")
        
        # ── 比對維度 A: error_pattern normalized key ──
        key = _normalize_error_key(error_pattern)
        if key:
            if key in existing_keys:
                logger.info(
                    f"[Dedup] 跳過重複 issue (error_key 已存在): "
                    f"key='{key}', title='{title[:60]}'"
                )
                continue
            if key in seen_keys:
                logger.info(
                    f"[Dedup] 跳過同批次重複 issue: "
                    f"key='{key}', title='{title[:60]}'"
                )
                continue
        
        # ── 比對維度 B: title 包含於現有內容 ──
        # 用 title 的 lowercase 在現有內容中做子字串搜尋
        # 注意：只搜尋夠長的 title（至少 10 字元），避免太短的 title 誤命中
        if title and len(title) >= 10 and title.lower() in existing_lower:
            logger.info(
                f"[Dedup] 跳過重複 issue (title 已存在): '{title[:60]}'"
            )
            continue
        
        # ── 通過去重 → 加入待寫入列表 ──
        new_issues.append(issue)
        if key:
            seen_keys.add(key)  # 記錄已接受的 key，防同批次重複
    
    skipped = len(known_issues) - len(new_issues)
    if skipped > 0:
        logger.info(
            f"[Dedup] 去重結果: 原始 {len(known_issues)} 條 → "
            f"去重後 {len(new_issues)} 條 (跳過 {skipped} 條重複)"
        )
    
    return new_issues


# ============================================================================
# MAIN ORCHESTRATOR
# 2026.02.28 George: v2.1 新增 skills-aware 決策邏輯
# ============================================================================

async def run_gatekeeper(
    payload: GatekeeperPayload,
    gatekeeper_agent: Any = None,
) -> GatekeeperDecision:
    """
    Gatekeeper 主流程（P0 版本）：規則分類 → 知識萃取 → 查重 → 寫回本地。
    
    2026.02.28 George: v2.1 Skills-Aware 決策邏輯：
    - 有參考 skills + 無 error → 完全跳過（skills 已發揮作用）
    - 有參考 skills + 有 error → 優先合併回已參考的 skill
    - 無參考 skills → 正常走完整流程
    
    Graceful degradation：
    - 如果 gatekeeper_agent 不可用 → 用規則層做知識萃取
    - 查重失敗 → 直接新建（最多多一個 skill，不會丟資料）
    - 任何步驟失敗都不影響使用者體驗
    """
    start_time = datetime.now(timezone.utc)
    logger.info(f"[Gatekeeper] 開始處理任務 {payload.task_id}")

    # ═══════════════════════════════════════════
    # PRE-CHECK: Skills-Aware 早期退出（v2.1 新增）
    # ═══════════════════════════════════════════

    if payload.skills_referenced and not payload.error_messages:
        # 有參考 skills 且一次成功 → 完全跳過
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        logger.info(
            f"[Gatekeeper] Skills 已發揮作用，跳過萃取 "
            f"(referenced: {payload.skills_referenced}, errors: 0)"
        )
        return GatekeeperDecision(
            task_id=payload.task_id,
            classification=ClassificationResult(
                primary_type=SkillType.KNOWLEDGE,
                confidence=1.0,
                has_knowledge_to_extract=False,
                scores={"knowledge": 0.0},
                suggested_action=GatekeeperAction.SKIP_SKILLS_EFFECTIVE,
            ),
            action_taken=GatekeeperAction.SKIP_SKILLS_EFFECTIVE,
            skill_written=None,
            reason=f"CodingAgent 參考了 {payload.skills_referenced} 且一次成功，無需萃取",
            processing_time_ms=int(elapsed),
        )

    if payload.skills_referenced and payload.error_messages:
        logger.info(
            f"[Gatekeeper] CodingAgent 參考了 {payload.skills_referenced} "
            f"但仍有 {len(payload.error_messages)} 個錯誤，將萃取新知識並優先合併回已有 skill"
        )

    # ═══════════════════════════════════════════
    # PHASE 1: Knowledge Gate（v2.6 簡化為 boolean）
    # ═══════════════════════════════════════════

    has_knowledge = _has_extractable_knowledge(payload)
    classification = _make_gate_classification(has_knowledge)
    logger.info(
        f"[Gatekeeper] Knowledge gate: has_knowledge={has_knowledge}, "
        f"error_count={len(payload.error_messages)}"
    )

    # P0：只處理有知識可萃取的情況
    if not has_knowledge:
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        logger.info(f"[Gatekeeper] 無知識可萃取，跳過 (errors: {len(payload.error_messages)})")
        return GatekeeperDecision(
            task_id=payload.task_id,
            classification=classification,
            action_taken=GatekeeperAction.SKIP,
            skill_written=None,
            reason=f"無執行錯誤，跳過知識萃取",
            processing_time_ms=int(elapsed),
        )

    # ═══════════════════════════════════════════
    # PHASE 2: 知識萃取
    # ═══════════════════════════════════════════

    knowledge = None

    # 優先用 LLM
    if gatekeeper_agent:
        knowledge = await extract_knowledge_with_llm(payload, gatekeeper_agent)

    # Fallback：規則層萃取
    if not knowledge:
        knowledge = extract_knowledge_by_rules(payload)

    if not knowledge:
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        logger.info("[Gatekeeper] 萃取不到有價值的知識，跳過")
        return GatekeeperDecision(
            task_id=payload.task_id,
            classification=classification,
            action_taken=GatekeeperAction.SKIP,
            skill_written=None,
            reason="萃取不到有價值的知識",
            processing_time_ms=int(elapsed),
        )

    logger.info(
        f"[Gatekeeper] 萃取到 {len(knowledge.get('known_issues', []))} 條知識, "
        f"skill_name={knowledge.get('skill_name')}"
    )

    # ═══════════════════════════════════════════
    # PHASE 3: 查重（v2.1: skills_referenced 優先合併）
    # 2026.03.04 George: v2.2 新增 relevance check，避免不相關知識被合併
    # ═══════════════════════════════════════════

    existing_skill_dir = None
    # 2026.08.24 George: v2.8 — 合併目標那份 SKILL.md 的 Blob ETag，供條件上傳用
    existing_skill_etag = None
    # 2026.08.24 George: v2.8 — Blob 與本地都找不到的 skills_referenced
    unresolved_refs: List[str] = []

    # 2026.02.28 George: v2.1 如果有 skills_referenced，優先嘗試合併回已參考的 skill
    # 2026.03.04 George: v2.2 加入 relevance check，不再盲目信任 skills_referenced
    # 2026.03.10 George: v2.4.1 修正 — 遍歷所有 skills_referenced 選最佳
    # 問題：原本迴圈用 break 取第一個找到的 skill（不管 relevance 高低），
    #   導致 ['azure-ad-...', 'outlook-...', 'azure-diagrams-tips'] 這種列表中，
    #   diagrams 相關的知識被合併到第一個碰巧 PASS 的 azure-ad skill
    # 修正：評估所有 skills_referenced 的 relevance score，選分數最高的
    if payload.skills_referenced:
        best_score = 0.0
        best_dir = None
        best_etag = None
        
        for ref_skill in payload.skills_referenced:
            # 2026.08.24 George: v2.8 — 改為先從 Blob 拉回 SKILL.md 當基準。
            # 底下 _is_orchestration_skill()、check_relevance_for_merge() 以及
            # 之後的 merge/版本遞增/dedup 全部讀這一份，基準舊了就全部一起錯。
            found_dir, found_etag = await _resolve_and_refresh_skill_dir(ref_skill)
            if not found_dir:
                unresolved_refs.append(ref_skill)
                logger.info(
                    f"[Gatekeeper] skills_referenced={ref_skill} 在 Blob 與本地皆找不到，跳過"
                )
                continue

            # 2026.08.21 George : 分階架構下,skills_referenced 會同時含 parent
            # 與 child。parent 是情境編排層,內容是分支與流程,不是可累積的
            # 除錯知識;而它的 tags/description 往往跟錯誤情境高度相關,
            # relevance score 很容易贏過真正出錯的 child。一旦贏了,
            # Gatekeeper Addendum 會靜靜寫進編排層並自動同步回 Blob。
            if _is_orchestration_skill(found_dir):
                logger.info(
                    f"[Gatekeeper] skills_referenced={ref_skill} 為編排層"
                    f"（有 metadata.children），不寫回知識，跳過"
                )
                continue

            # v2.4.1: 取得 relevance score（不再是 bool）
            score = check_relevance_for_merge(knowledge, found_dir)
            
            if score > best_score:
                best_score = score
                best_dir = found_dir
                best_etag = found_etag
                logger.info(
                    f"[Gatekeeper] 候選 Skill 更新: {ref_skill} "
                    f"score={score:.1f} (目前最佳)"
                )
        
        if best_dir:
            existing_skill_dir = best_dir
            existing_skill_etag = best_etag
            logger.info(
                f"[Gatekeeper] 最終選擇合併目標: {os.path.basename(best_dir)} "
                f"(score={best_score:.1f}, 從 {len(payload.skills_referenced)} 個候選中選出)"
            )
        else:
            logger.info(
                f"[Gatekeeper] 所有 skills_referenced 均不相關 "
                f"(scores all = 0)，將走一般查重流程"
            )

    # ── 2026.08.24 George: v2.8 保守護欄 ──
    # skills_referenced 有值卻一個都解析不到時，正確的合併目標依定義不在本地候選
    # 集內，check_duplicate() 再怎麼比對都只會命中「別的 skill」——而它的門檻只有
    # 3 個共同 token、_tokenize 又沒有 stopword 過濾，同領域命名幾乎必中。
    # 例：azure-table-storage-cleanup 的知識會被併進
    # faea-usage-report-from-azure-table-storage（光名字就有 3 個 token 重疊）。
    review_mode = os.environ.get("SKILL_REVIEW_MODE", "auto").lower()
    blocked_by_unresolved = bool(unresolved_refs) and not existing_skill_dir

    if blocked_by_unresolved and review_mode != "manual":
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        logger.warning(
            f"[Gatekeeper] skills_referenced 全數無法解析 {unresolved_refs}，"
            f"放棄寫回以避免誤併到無關 skill"
        )
        return GatekeeperDecision(
            task_id=payload.task_id,
            classification=classification,
            action_taken=GatekeeperAction.SKIP,
            skill_written=None,
            reason=f"skills_referenced 無法解析（{', '.join(unresolved_refs)}），跳過寫回",
            processing_time_ms=int(elapsed),
        )

    # 如果 skills_referenced 沒找到，走一般查重
    if not existing_skill_dir and not blocked_by_unresolved:
        try:
            existing_skill_dir = check_duplicate(
                knowledge.get("skill_name", ""),
                knowledge.get("skill_description", ""),
            )
            if existing_skill_dir:
                # 2026.08.21 George : 同上 —— 一般查重也可能命中編排層,一併擋掉,
                # 落回「開新 skill」比污染編排層安全。
                if _is_orchestration_skill(existing_skill_dir):
                    logger.info(
                        f"[Gatekeeper] 查重命中 {os.path.basename(existing_skill_dir)} "
                        f"為編排層，不寫回知識，改走新建流程"
                    )
                    existing_skill_dir = None
                else:
                    logger.info(f"[Gatekeeper] 找到相似 Skill: {existing_skill_dir}，將合併")
        except Exception as e:
            logger.warning(f"[Gatekeeper] 查重失敗，將新建: {e}")

    # ═══════════════════════════════════════════
    # PHASE 4: 寫回本地 skills/ 目錄
    # 2026.03.30 George: v2.6 HITL — 加入 SKILL_REVIEW_MODE 分支
    # ═══════════════════════════════════════════

    skill_dir = None
    pending_id = None  # HITL

    # HITL — 審核模式已在 PHASE 3 護欄處讀取（review_mode）

    try:
        # HITL — manual 模式：寫 pending + 通知 Logic App，不直接寫入正式目錄
        if review_mode == "manual":
            pending_id = await _write_to_pending(
                knowledge=knowledge,
                existing_skill_dir=existing_skill_dir,
                classification=classification,
                payload=payload,
            )
            await _notify_logic_app(pending_id, knowledge, existing_skill_dir)
            action_taken = GatekeeperAction.PENDING_REVIEW
            if payload.skills_referenced and existing_skill_dir:
                reason = (
                    f"待審核 — 預計合併回 {os.path.basename(existing_skill_dir)}"
                )
            else:
                reason = f"待審核 — 預計{'更新' if existing_skill_dir else '新建'} Knowledge Skill"
        else:
            # auto 模式：維持現有行為，直接寫入
            skill_dir = write_knowledge_skill(
                knowledge,
                existing_skill_dir=existing_skill_dir,
            )
            action_taken = GatekeeperAction.UPDATE_KNOWLEDGE
            # 2026.02.28 George: v2.1 在 reason 中標註是否為 skills-aware merge
            if payload.skills_referenced and existing_skill_dir:
                reason = f"Skills-aware 合併回 {os.path.basename(existing_skill_dir)}"
            else:
                reason = f"{'更新' if existing_skill_dir else '新建'} Knowledge Skill"
    except Exception as e:
        logger.error(f"[Gatekeeper] 寫回失敗: {e}")
        action_taken = GatekeeperAction.SKIP
        reason = f"寫回失敗: {e}"

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
    logger.info(
        f"[Gatekeeper] 完成: action={action_taken.value}, "
        f"skill={skill_dir}, pending={pending_id}, 耗時 {elapsed:.0f}ms"
    )

    return GatekeeperDecision(
        task_id=payload.task_id,
        classification=classification,
        action_taken=action_taken,
        skill_written=skill_dir,
        reason=reason,
        processing_time_ms=int(elapsed),
        pending_id=pending_id,
        # 2026.08.14 George: 取目錄名而非 knowledge["skill_name"] — merge 時實際落地的是
        # 既有 skill 的目錄名，用前者才不會在 SQL 多開一列不存在的 skill。
        skill_name=os.path.basename(skill_dir) if skill_dir else None,
        # 2026.08.24 George: 只有「基準確實來自 Blob 的 merge」才帶 ETag；新建或
        # 走模糊查重命中的路徑維持無條件覆蓋（不在本次範圍）。
        skill_md_etag=existing_skill_etag if skill_dir else None,
    )


# ============================================================================
# PHASE 4 HITL: Pending 暫存 + Logic App 通知
# 2026.03.30 George: v2.6
#
# manual 模式下，PHASE 4 不直接寫入正式 skills/ 目錄，
# 而是將 knowledge JSON + 決策 context 寫到 Blob skills-pending/，
# 並通知 Logic App 推送 Adaptive Card 到 Teams 供人工審核。
#
# 關鍵設計：pending 存的是 knowledge JSON 而非最終 SKILL.md。
# approve 時才呼叫 write_knowledge_skill() 對當下最新的正式 skill 做 merge，
# 確保多個 pending 同時存在時不會後蓋前。
# ============================================================================

# Pending Blob 路徑前綴
PENDING_BLOB_PREFIX = "skills-pending"
# Logic App HTTP trigger URL（manual 模式必填）
LOGIC_APP_SKILL_REVIEW_URL = os.environ.get("LOGIC_APP_SKILL_REVIEW_URL", "")


async def _write_to_pending(
    knowledge: Dict,
    existing_skill_dir: Optional[str],
    classification: "ClassificationResult",
    payload: "GatekeeperPayload",
) -> str:
    """
    2026.03.30 George: v2.6 HITL 新增

    將 knowledge JSON + 決策 context 序列化寫到 Blob skills-pending/{pending_id}/。

    存 knowledge JSON 而非最終 SKILL.md 的原因：
    - approve 時才對「當下最新」的正式 SKILL.md 做 merge
    - 多個 pending 指向同一個 skill 時不會互相覆蓋
    - _dedup_known_issues() 在 approve merge 時自然生效

    Returns:
        pending_id (str): 唯一識別碼，供 approve/reject callback 使用
    """
    from skills_sync import blob_write_pending  # 延遲 import，避免循環依賴

    pending_id = f"pending-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    metadata = {
        "pending_id": pending_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "task_id": payload.task_id,

        # ── 核心：knowledge JSON（approve 時傳給 write_knowledge_skill()）──
        "knowledge": knowledge,

        # ── 決策 context（approve 時需要）──
        "existing_skill_dir": (
            os.path.basename(existing_skill_dir) if existing_skill_dir else None
        ),
        "action_type": "merge" if existing_skill_dir else "create",
        "relevance_score": None,  # 由呼叫端補充（如果有的話）
        "classification": classification.primary_type.value,

        # ── 審核輔助資訊（顯示在 Adaptive Card 上）──
        "user_request": payload.user_request[:500],
        "skills_referenced": payload.skills_referenced,
    }

    await blob_write_pending(pending_id, metadata)

    logger.info(
        f"[Gatekeeper] HITL: 寫入 pending — "
        f"id={pending_id}, skill={knowledge.get('skill_name')}, "
        f"action={'merge → ' + os.path.basename(existing_skill_dir) if existing_skill_dir else 'create'}"
    )

    return pending_id


async def _notify_logic_app(
    pending_id: str,
    knowledge: Dict,
    existing_skill_dir: Optional[str],
) -> None:
    """
    2026.03.30 George: v2.6 HITL 新增

    HTTP POST 到 Logic App trigger URL，觸發 Adaptive Card 推送到 Teams。

    Graceful degradation：
    - Logic App URL 未設定 → log warning，不 raise
    - HTTP 失敗 → log error，不 raise（pending 已寫入 Blob，可手動審核）
    """
    if not LOGIC_APP_SKILL_REVIEW_URL:
        logger.warning(
            "[Gatekeeper] HITL: LOGIC_APP_SKILL_REVIEW_URL 未設定，"
            "跳過 Logic App 通知（pending 已寫入 Blob，需手動審核）"
        )
        return

    # ── 組裝 Adaptive Card 需要的摘要 ──
    # 把 issues_summary 從 array 改成純文字
    issues_text_lines = []
    for issue in knowledge.get("known_issues", [])[:5]:
        title = issue.get("title", "")[:100]
        error = issue.get("error_pattern", "")[:150]
        issues_text_lines.append(f"**{title}**\n`{error}`")    

    card_payload = {
        "pending_id": pending_id,
        "skill_name": knowledge.get("skill_name", "unknown"),
        "skill_description": knowledge.get("skill_description", "")[:200],
        "action_type": "merge" if existing_skill_dir else "create",
        "merge_target": (
            os.path.basename(existing_skill_dir) if existing_skill_dir else None
        ),
        "issues_count": len(knowledge.get("known_issues", [])),
        "issues_text": "\n\n".join(issues_text_lines) if issues_text_lines else "無",
        "tags": knowledge.get("tags", [])[:10],
        "useful_packages": knowledge.get("useful_packages", [])[:10],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                LOGIC_APP_SKILL_REVIEW_URL,
                json=card_payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status < 300:
                    logger.info(
                        f"[Gatekeeper] HITL: Logic App 通知成功 — "
                        f"pending_id={pending_id}, status={resp.status}"
                    )
                else:
                    body = await resp.text()
                    logger.error(
                        f"[Gatekeeper] HITL: Logic App 通知失敗 — "
                        f"status={resp.status}, body={body[:200]}"
                    )
    except Exception as e:
        logger.error(
            f"[Gatekeeper] HITL: Logic App 通知異常 — "
            f"pending_id={pending_id}, error={type(e).__name__}: {e}"
        )
        # 不 raise — pending 已寫入 Blob，可透過 /api/skills/approve 手動審核


# ============================================================================
# INTEGRATION POINT: 與 CodeAgentWorkflow 的銜接
# 2026.02.28 George: v2.1 新增 skills_referenced 參數
# ============================================================================

# ============================================================================
# INTEGRATION POINT: 與 CodeAgentWorkflow 的銜接
# 2026.03.04 George: v2.3 修正 return 值
# ============================================================================

async def trigger_gatekeeper_async(
    state,                          # ConversationState from main module
    user_request: str,
    gatekeeper_agent: Any = None,   # Agent instance（可選，有就用 LLM）
    skills_referenced: List[str] = None,  # v2.1: CodingAgent 參考了哪些 skills
) -> Optional[GatekeeperDecision]:  # v2.3: 新增回傳型別
    """
    非同步觸發 Gatekeeper。
    
    2026.03.04 George: v2.3 
    回傳 GatekeeperDecision 以便 code_agent_hosted.py 判斷是否需要同步到 Blob。
    """
    try:
        payload = GatekeeperPayload.from_conversation_state(
            state, user_request,
            skills_referenced=skills_referenced or [],
        )

        if not payload.final_code:
            logger.info("[Gatekeeper] 無最終程式碼，跳過")
            return None

        decision = await run_gatekeeper(
            payload=payload,
            gatekeeper_agent=gatekeeper_agent,
        )

        # 控制台日誌輸出
        if decision.action_taken == GatekeeperAction.SKIP_SKILLS_EFFECTIVE:
            print(
                f"[Gatekeeper] ✅ Skills 已生效，跳過萃取 "
                f"(referenced: {payload.skills_referenced}) "
                f"({decision.processing_time_ms}ms)"
            )
        # 2026.03.30 George: v2.6 HITL — 新增 PENDING_REVIEW 分支
        elif decision.action_taken == GatekeeperAction.PENDING_REVIEW:
            print(
                f"[Gatekeeper] ⏳ 待審核: {decision.reason} "
                f"(pending_id={decision.pending_id}) "
                f"({decision.processing_time_ms}ms)"
            )
        elif decision.skill_written:
            print(
                f"[Gatekeeper] ✅ {decision.reason}: "
                f"{os.path.basename(decision.skill_written)} "
                f"({decision.processing_time_ms}ms)"
            )
        else:
            print(
                f"[Gatekeeper] ⏭ 跳過: {decision.reason} "
                f"({decision.processing_time_ms}ms)"
            )
            
        return decision # v2.3: 關鍵修正，必須回傳結果

    except Exception as e:
        logger.error(f"[Gatekeeper] 非同步處理失敗: {e}")
        print(f"[Gatekeeper] ⚠ 背景處理失敗: {e}")
        return None

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _parse_json_response(text: str) -> Optional[Dict]:
    """解析 LLM 回應中的 JSON"""
    if not text:
        return None
    text = text.strip()
    for prefix in ["```json", "```"]:
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.endswith("```"):
        text = text[:-3]
    try:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            return json.loads(text[start:end + 1])
    except Exception:
        pass
    return None


def _sanitize_skill_name(name: str) -> str:
    """
    確保 skill name 符合 Agent Skills 規範：
    - 小寫字母、數字、連字號
    - 不以連字號開頭或結尾
    - 不含連續連字號
    - 最多 64 字元
    """
    name = name.lower().strip()
    name = re.sub(r'[^a-z0-9\-]', '-', name)
    name = re.sub(r'-+', '-', name)             # 去連續連字號
    name = name.strip('-')                       # 去頭尾連字號
    name = name[:64]
    return name or "auto-skill"


def _escape_yaml_string(s: str) -> str:
    """Escape YAML 字串中的特殊字元"""
    return s.replace('"', '\\"').replace('\n', ' ')


def _generate_skill_name_from_request(user_request: str) -> str:
    """從使用者需求中產生 skill name"""
    # 提取英文關鍵字
    words = re.findall(r'[a-zA-Z]+', user_request.lower())
    # 過濾太短和太常見的詞
    stop_words = {
        'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
        'could', 'should', 'may', 'might', 'can', 'to', 'of', 'in',
        'for', 'on', 'with', 'at', 'by', 'from', 'and', 'or', 'not',
        'use', 'using', 'make', 'create', 'write', 'help', 'me', 'i',
        'please', 'want', 'need', 'python', 'code',
    }
    keywords = [w for w in words if len(w) >= 3 and w not in stop_words][:4]
    return "-".join(keywords) if keywords else "auto-generated"


def _extract_package_names(code: str) -> List[str]:
    """從程式碼中提取 import 的套件名稱"""
    packages = set()
    for match in re.finditer(r'^(?:import|from)\s+([\w\.]+)', code, re.MULTILINE):
        pkg = match.group(1).split('.')[0]
        if pkg not in {'os', 'sys', 'json', 're', 'math', 'datetime',
                       'pathlib', 'typing', 'collections', 'functools',
                       'itertools', 'string', 'io', 'copy', 'time',
                       'logging', 'hashlib', 'uuid', 'subprocess', 'glob',
                       'traceback', 'enum', 'dataclasses', 'abc',
                       'contextlib', 'warnings', 'textwrap', 'shutil'}:
            packages.add(pkg)
    return sorted(packages)


def _extract_tags(payload: GatekeeperPayload) -> List[str]:
    """
    從 payload 中提取技術標籤，用於 check_relevance_for_merge 維度 1 比對
    以及 _create_new_skill 寫入 **相關技術** 行。
    
    2026.03.10 George: v2.4 重寫 — 移除 hardcoded tech_keywords
    - 舊版問題：維護一個靜態的 tech_keywords 列表不切實際，
      新套件/技術不會自動被收錄，導致 tags 覆蓋率隨時間遞減
    - 新版策略：從兩個動態來源提取 tags
      1. _extract_package_names(final_code) — 從 import 語句提取實際用到的套件
         → 最可靠，反映「這次任務實際用了什麼」
      2. user_request 中的有意義關鍵字 — 從使用者需求提取技術相關詞彙
         → 補充套件名以外的技術概念（如 "chart", "diagram", "api"）
    - 效果：零維護成本，新套件自動被收錄，且比 hardcoded list 更精準
    """
    tags = set()
    
    # ── 來源 1: 從 import 語句提取實際使用的套件名 ──
    # _extract_package_names 已排除 stdlib（os, sys, json 等），
    # 只保留第三方套件，正好是我們需要的技術標籤
    imported_packages = _extract_package_names(payload.final_code)
    tags.update(p.lower() for p in imported_packages)
    
    # ── 來源 2: 從 user_request 提取有意義的技術關鍵字 ──
    # 用途：捕捉 import 語句抓不到的概念（如使用者說「畫一個架構圖」
    # → "架構圖" 本身不是套件名，但 "diagram" 可能出現在 request 中）
    # 過濾策略：只取英文 token，排除太短（<3 字元）和太泛的詞
    request_stop_words = {
        # 常見動詞/助詞（不具技術辨識度）
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "can", "to", "of", "in",
        "for", "on", "with", "at", "by", "from", "and", "or", "not",
        "use", "using", "make", "create", "write", "help", "me",
        "please", "want", "need", "get", "set", "add", "new", "try",
        "run", "test", "check", "show", "list", "find", "read",
        "this", "that", "these", "those", "then", "than", "also",
        "into", "about", "just", "like", "some", "any", "all", "each",
        "how", "what", "when", "where", "which", "who", "why",
        "its", "your", "our", "my", "the", "via", "per", "but",
        # 太泛的技術詞（在 check_relevance_for_merge 中也會被 generic_tags 過濾）
        "code", "file", "data", "error", "bug", "fix", "issue",
        "script", "program", "function", "class", "module",
    }
    request_tokens = re.findall(r'[a-zA-Z]+', payload.user_request.lower())
    for token in request_tokens:
        if len(token) >= 3 and token not in request_stop_words:
            tags.add(token)
    
    return sorted(tags)


def _tokenize(text: str) -> List[str]:
    """將文字切成小寫 token 用於查重"""
    if not text:
        return []
    return [w.lower() for w in re.findall(r'[a-zA-Z\u4e00-\u9fff]+', text) if len(w) >= 2]


def _parse_skill_frontmatter(content: str) -> tuple:
    """
    從 SKILL.md 解析 YAML frontmatter 欄位。
    
    2026.03.05 George: v2.3 擴充
    - 原本只回傳 (name, description) 二元組
    - 現在回傳 (name, description, metadata_dict) 三元組
    - metadata_dict 包含 author, version, skill_type 等巢狀欄位
    - 向後相容：既有呼叫端用 name, desc = _parse_skill_frontmatter(...)
      會自動忽略第三個值（Python tuple unpacking 特性），
      但為安全起見，所有呼叫端已同步更新
    
    支援的 frontmatter 格式範例：
    （A）Gatekeeper 自產格式：
        ---
        name: azure-diagrams-tips
        description: "..."
        metadata:
          author: gatekeeper-auto
          version: "1.0"
          skill_type: knowledge
        ---
    
    （B）人工撰寫格式（如 outlook-calendar-management.md）：
        ---
        name: outlook-calendar-management
        description: 專用於透過 Microsoft Graph API...
        version: 1.0.0
        ---
    """
    name = ""
    description = ""
    metadata = {}  # 2026.03.05 v2.3: 新增 metadata dict
    
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            frontmatter = content[3:end]
            in_metadata_block = False  # 追蹤是否在 metadata: 巢狀區塊內
            
            for line in frontmatter.split("\n"):
                stripped = line.strip()
                
                # ── 偵測 metadata: 巢狀區塊的開始 ──
                if stripped == "metadata:":
                    in_metadata_block = True
                    continue
                
                # ── 巢狀區塊內的縮排行（以空格開頭）──
                if in_metadata_block and line.startswith(("  ", "\t")):
                    if ":" in stripped:
                        key, val = stripped.split(":", 1)
                        metadata[key.strip()] = val.strip().strip('"\'')
                    continue
                else:
                    # 非縮排行 → 離開 metadata 區塊
                    in_metadata_block = False
                
                # ── 頂層欄位解析 ──
                if stripped.startswith("name:"):
                    name = stripped.split(":", 1)[1].strip().strip('"\'')
                elif stripped.startswith("description:"):
                    description = stripped.split(":", 1)[1].strip().strip('"\'')
                elif stripped.startswith("version:"):
                    # 2026.03.05 v2.3: 頂層 version（人工撰寫格式常見）
                    metadata["version"] = stripped.split(":", 1)[1].strip().strip('"\'')
    
    return name, description, metadata


# 2026.03.05 George: v2.3 新增 ─ 偵測 SKILL.md 的來源類型
# ============================================================================
# 用途：在 merge 時決定使用哪套策略
# - "gatekeeper-auto": Gatekeeper 自動產生的 skill，有固定 heading 結構
# - "manual": 人工撰寫的 skill，heading 結構不可預測
# ============================================================================

def _detect_skill_origin(content: str, metadata: Dict = None) -> str:
    """
    偵測 SKILL.md 是 Gatekeeper 自動產生還是人工撰寫。
    
    判斷邏輯（優先順序）：
    1. metadata.author == "gatekeeper-auto" → 確定是自動產生
    2. 包含 Gatekeeper 特有的結構 marker → 推定為自動產生
    3. 以上皆無 → 視為人工撰寫
    
    Returns:
        "gatekeeper-auto" 或 "manual"
    """
    # ── 方式 1: 透過 metadata.author 判斷（最可靠）──
    if metadata and metadata.get("author") == "gatekeeper-auto":
        return "gatekeeper-auto"
    
    # ── 方式 2: 透過結構特徵推定 ──
    # Gatekeeper 自產的 SKILL.md 一定包含這些 heading（見 _create_new_skill）
    gatekeeper_markers = [
        "## 已知問題與 Workaround",
        "## ⚠️ 注意事項",
        "<!-- Gatekeeper 自動更新:",       # merge 時插入的 HTML comment
    ]
    marker_hits = sum(1 for m in gatekeeper_markers if m in content)
    
    # 至少命中 2 個 marker 才算（避免人工檔案偶然包含某一個 heading）
    if marker_hits >= 2:
        return "gatekeeper-auto"
    
    return "manual"


# 2026.03.05 George: v2.3 新增 ─ 通用版本號解析與遞增
# ============================================================================
# 解決問題：原本 merge 時用 re.search(r'version:\s*"(\d+)\.(\d+)"') 硬找版本號，
# 只認 "1.0" 格式（帶雙引號、兩段式），對人工撰寫的 1.0.0（semver、無引號）無效
# ============================================================================

def _parse_version_string(version_str: str) -> tuple:
    """
    解析版本號字串，相容多種格式。
    
    支援格式：
    - "1.0"      → (1, 0, None)    Gatekeeper 預設
    - 1.0.0      → (1, 0, 0)       semver 三段式
    - "2.3"      → (2, 3, None)    帶引號兩段式
    - 1.2.3-beta → (1, 2, 3)       忽略 pre-release suffix
    
    Returns:
        (major, minor, patch_or_none) tuple
        如果解析失敗回傳 None
    """
    if not version_str:
        return None
    
    # 去引號和空白
    clean = version_str.strip().strip('"\'').strip()
    
    # 移除 pre-release suffix（如 -beta, -rc1）
    clean = re.split(r'[-+]', clean)[0]
    
    parts = clean.split('.')
    try:
        if len(parts) == 3:
            return (int(parts[0]), int(parts[1]), int(parts[2]))
        elif len(parts) == 2:
            return (int(parts[0]), int(parts[1]), None)
    except ValueError:
        pass
    return None


def _increment_version(version_str: str) -> str:
    """
    遞增版本號的最後一段，保持原有格式。
    
    範例：
    - "1.0"   → "1.1"
    - 1.0.0   → 1.0.1
    - "2.3"   → "2.4"
    - 1.2.3   → 1.2.4
    
    如果解析失敗，回傳原字串不做修改。
    """
    parsed = _parse_version_string(version_str)
    if not parsed:
        return version_str
    
    major, minor, patch = parsed
    
    # 判斷原始格式是否帶引號
    stripped = version_str.strip()
    has_quotes = stripped.startswith('"') or stripped.startswith("'")
    
    if patch is not None:
        # 三段式 semver → 遞增 patch
        new_ver = f"{major}.{minor}.{patch + 1}"
    else:
        # 兩段式 → 遞增 minor
        new_ver = f"{major}.{minor + 1}"
    
    # 保持原有引號格式
    if has_quotes:
        quote_char = stripped[0]
        return f"{quote_char}{new_ver}{quote_char}"
    return new_ver