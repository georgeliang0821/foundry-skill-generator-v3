"""
skills_provider_factory.py — Per-turn, per-user SkillsProvider builder
====================================================================

職責:
    對齊 MS OpenClaw Mode B 架構 (DYNAMIC_SKILLS_ENABLED=true 模式):
    1. 用 user OBO Azure SQL token 查 v_my_skills(RLS 自動過濾)
    2. 從 Blob Storage 撈該 user 有權限的 skill 資料夾(SKILL.md +
       references/ + assets/;in-process cache)
    3. Materialize 到 per-turn 臨時目錄
    4. 回傳 file-based SkillsProvider(對齊既有 FileAgentSkillsProvider pattern)
    5. async context manager,scope 結束自動清理

使用範例(在 CodeAgentWorkflow.run 內,由 core_handler 注入):

    sql_token = state.user_data.get("AZURE_SQL_ACCESS_TOKEN")
    async with self.skills_factory.create_provider_scope(
        sql_token=sql_token,
        session_id=state.session_id,
        turn_id=str(state.job_id or uuid.uuid4()),
    ) as provider:
        ...

故障行為(binary flag 設計,故意不做 fallback):
    - 沒拿到 sql_token         → yield 空 provider
    - SQL fetch 失敗            → yield 空 provider(log error)
    - Blob materialize 失敗     → yield 空 provider(log error)
    - User 沒有任何 grant       → yield 空 provider
    Turn 仍能跑完,但 LLM 看不到任何 advertised skill。
    Ops 處理方式:把 ACA env var DYNAMIC_SKILLS_ENABLED 改 false 建新 revision,
                  退回 static 模式(本地 skill 全開)。

依賴:
    - aioodbc + pyodbc 連 Azure SQL(用 token authentication,
      driver "ODBC Driver 18 for SQL Server")
    - azure-storage-blob[aio]
    - cachetools

環境變數:
    - AZURE_SQL_SERVER           (e.g. xxx.database.windows.net)
    - AZURE_SQL_DATABASE         (e.g. openclaw_meta)
    - AZURE_STORAGE_ACCOUNT_NAME    (e.g. https://xxx.blob.core.windows.net)
                                 (由 core_handler.startup 注入 BlobServiceClient)
    - SKILLS_BLOB_CONTAINER      (預設 "skills")
    - SKILLS_MATERIALIZE_BASE    (預設 /tmp/openclaw_skills)

VERSION: 1.14
2026.09.04 George : v1.14 — scenario 白名單收斂
                            - `_materialize_skills(scenario=)`:宿主帶入情境層
                              skill 名稱後,只落地它 metadata.children 列到的
                              skill。⚠️ 是**白名單**:不分 is_internal,parent
                              沒指名的一律不落地。
                            - 因此 children 的語義從「指名的能力層 child」擴大成
                              「本情境的完整依賴清單」。流程中會用到的通用 skill
                              也必須列進去。
                            - 重要性在 N>1:同情境 sibling 彼此高度相似,而後端
                              只有使用者原話 + 扁平 skill 池兩個訊號(parent 的編排
                              指引不落地、credentials key 名也不進 prompt),不收斂
                              就沒有任何依據分辨該用哪一個。

VERSION: 1.13
2026.08.22 George : v1.13 — 情境層 parent skill 不進 runtime materialize
                            - _materialize_skills() 跳過 frontmatter 宣告了
                              metadata.children 的 skill。parent 正文寫的是宿主
                              該做的事(讀行事曆、多輪接續),後端沒有那些能力,
                              載進來只會跟同領域的 child 搶關鍵字然後執行不了。
                            - ⭐ 判準是「自己宣告 children」= parent,不是「被別人
                              列為 child」。寫反會擋掉真正要執行的 child,整條
                              委派路徑斷掉且沒有任何錯誤訊息。
                            - 這是 v1.12 那個 is_internal 投影的鏡像、方向相反:
                              is_internal 把 child 藏離宿主目錄(core_handler 做),
                              本版把 parent 藏離 runtime(這一層做)。兩者互不
                              重疊,合起來才是完整的分層。
                            - 不影響 fetch_skill:get_skill_content() 直接讀 Blob,
                              不吃 materialize 出來的目錄。
                            - 取代了 parent 正文裡「若後端誤載請改載 child」那段
                              請求式防護 —— 那要等誤載發生後才讀得到,而內部
                              agent 選技能時只看得到 name + description。
VERSION: 1.12
2026.08.21 George : v1.12 — Skill Registry 分階(is_internal)+ get_skill_content()
                            - v_my_skills 新增 is_internal 欄位(schema v2.2),
                              SkillMetadata 跟著加。情境層 parent skill 對外可見,
                              能力層 child 標 internal 後不出現在 list_skills,但
                              runtime 仍載得到(由 parent 正文指名)。
                            - ⭐ 過濾刻意「不」做在這一層。_fetch_allowed_skills()
                              同時服務 list_allowed_skills_full()(對外目錄)與
                              create_provider_scope()(MAF runtime materialize),
                              在這裡濾掉 internal 會連 runtime 一起殺掉,parent
                              指名後就拿不到了。is_internal 只是被帶出去,
                              投影由 core_handler.list_skills() 負責。
                            - list_allowed_skills_full() 回傳的 dict 新增
                              "_is_internal";底線前綴讓 _format_skills_catalog
                              自動略過,不會外洩給宿主。
                            - 新增 get_skill_content():供 MCP fetch_skill tool 取
                              單一 skill 的 SKILL.md 正文(progressive disclosure)。
                              重用 _fetch_skill_content() 的 TTL cache。
                            - ⚠ 部署順序:本版的 SELECT 已含 is_internal,未跑
                              schema v2.2 migration 的環境會 Msg 207 → fail-closed
                              成空 provider。先跑 SQL migration 再上 code。
VERSION: 1.11
2026.08.15 George : v1.11 — read_skill_resource 的 resource_name 前綴容錯
                            - v1.10 解決了「掃不到」,但還剩「名字對不上」:
                              MAF 註冊的 resource name 是相對 skill 根目錄的
                              完整路徑(assets/style.css),而 get_resource() 是
                              完全比對、無 basename fallback → SKILL.md 寫
                              style.css 就必失敗。
                            - 不以「要求所有 skill 作者寫對前綴」作為解法:
                              約定無法強制,而寫錯的懲罰是静默降級(模型改用
                              open() 繞路,產出看似成功卻沒套到樣式)。
                              改在讀取端解析:新增 PrefixTolerantSkillsProvider
                              覆寫 _read_skill_resource,查不到時用同一個 basename
                              去試各個已知目錄(沿用 derive_resource_scan_args 的
                              推導結果),兩種寫法都能命中。
                            - 只用 public API(get_resource / _find_skill),
                              不碰 FileSkill._resources。Mode A 已同步。
                            ⚠ 依賴 MAF 把 read_skill_resource tool 綁定到
                              self._read_skill_resource(_skills.py L2120),升級
                              套件時需重驗這個 dispatch 點仍在。
VERSION: 1.10
2026.08.15 George : v1.10 — 讓 skill 資產(references/ assets/)真的讀得到
                            - 症狀:read_skill_resource 一律回 "Error: Resource
                              '...' not found in skill '...'",即使檔案已經
                              materialize 到磁碟。
                            - 根因在 MAF 1.8.0 的掃描層,不在本檔的下載層:
                              from_paths 預設只認 7 種副檔名(不含 .css/.html)
                              且只掃 references/ 與 assets/ 兩個目錄、不遞迴。
                            - 修法:不寫死副檔名清單(那只是把 hardcode 從 MAF
                              搬到自家),而是用 skills_sync.derive_resource_scan_args()
                              從【實際落地的檔案】反推兩個參數並傳給 from_paths
                              —— 白名單被自己的輸入撐滿,形同 no-op,
                              select_skill_files() 回到唯一規則的位置。
                              Mode A(core_handler._build_static_skills_provider)
                              已同步修改,兩模式不可只改一邊。
                            ⚠ MAF 1.8.0 實測註記(升級套件後必須重驗):
                              - from_paths 【沒有】disable_load_skill_approval /
                                disable_read_skill_resource_approval /
                                disable_run_skill_script_approval 這三個參數,
                                傳了會 TypeError → 被下方 except 吞成空 provider。
                                唯一的 approval 旋鈕是 require_script_approval
                                (預設 False = 不需審批),且 load_skill /
                                read_skill_resource 本來就沒設 approval_mode。
                              - FileSkill.get_content() 【不會】輸出 <resources>
                                (只有 Inline/ClassSkill 會),模型無法發現資源名
                                → SKILL.md 必須自行寫出完整相對路徑
                                (如 assets/style.css,不可只寫檔名)。
                              - 資源以 read_text(utf-8) 讀取 → 二進位資產
                                (png/jpg 等)註冊得成但讀取必失敗。
VERSION: 1.9
2026.08.14 George : v1.9 — Skill Folder 格式支援(SKILL.md + references/ + assets/)
                            - v_my_skills 新增 blob_prefix 欄位(schema v2.1),
                              SkillMetadata 跟著加。blob_path 指向單一檔案,拿不到
                              整個資料夾;blob_prefix 才能拿來 list_blobs。
                            - row 取值改用欄位名稱(r.blob_prefix)而非位置索引,
                              view / SELECT 欄位順序異動不再造成靜默錯位。
                            - _materialize_skills() 從「下載單一 blob 寫成 SKILL.md」
                              改為「列舉 blob_prefix、依相對路徑還原整個資料夾」。
                            - 新增 _listing_cache:檔案清單跟內容一樣用
                              (skill_key, updated_at) 當 key。沒這層快取的話,
                              每一輪每個 skill 都要多一趟 list_blobs round-trip。
                            - 過濾規則(scripts/ 排除、檔數/容量上限、path traversal
                              防護)不在本檔實作,從 skills_sync import —— static 模式
                              與 Mode B 共用同一套,否則同一個 skill 在兩個模式下會
                              長得不一樣。
                            - _fetch_skill_content() / list_allowed_skills_full()
                              刻意不動:繼續走 blob_path 直取 SKILL.md。SKILL.md 是
                              唇唇被讀的熱點,值得繼續享受它自己那層內容 cache。
VERSION: 1.8
2026.08.14 George : v1.8 — 對齊 Skill RBAC schema v2(skill_key PK / owner_upn /
                            computed blob_path;version、description 欄位移除)
                            - v_my_skills 已無 description 欄位,原本的 SELECT 會直接
                              炸 Msg 207 → 整條讀路徑失效。改查
                              skill_name/owner_upn/skill_key/blob_path/updated_at。
                            - SkillMetadata 拿掉 description(該欄位自 v1.6 起就只被
                              建構、從未被讀取,description 一律從 Blob frontmatter 取),
                              新增 owner_upn(dedupe 用)與 skill_key(cache key 用)。
                            - ⭐ 資安關鍵:_content_cache 是跨 user 共享的 class-level
                              cache,key 原本用 skill_name。v2 下同名的全域版與某人的
                              私有版會撞 key → 內容跨使用者外洩。改用 skill_key。
                            - 新增 _dedupe_private_over_global():v_my_skills 對同一個
                              skill_name 可能回兩列(全域 + 私有)且刻意不做 tie-break,
                              由本檔決定「私有優先」。沒有這步,_materialize_skills 的兩個
                              write_one() 會在 gather 下競寫同一路徑,勝者不確定。
                            - is_public / skill_scope 刻意不放進 SkillMetadata:呈現層
                              決定不顯示,放了就是 dead field。
VERSION: 1.7
2026.07.20 George : v1.7 — RLS 清單快取 + 診斷查詢 debug gate (效能 #3)
                            - _fetch_allowed_skills 新增 per-principal 快取:
                              key = sha256(sql_token)(唯一識別 token 背後的
                              使用者,跟 #2 assertion hash 同思路)。⭐ 資安關鍵:
                              絕不能用 session_id 當 key,否則 A 的清單會被 B 複用。
                            - 雙軌失效:短 TTL(SKILLS_RLS_CACHE_TTL 預設 300s)保底
                              + Mode B publish(approve_pending_skill)主動
                              invalidate_rls_cache() — 發佈後下一 turn 立刻生效。
                            - 診斷查詢 SELECT SUSER_SNAME()... 改由
                              SKILLS_RLS_DIAG_ENABLED gate(正式環境預設關閉,
                              省每 turn 一趟 round-trip)。
                            - hit/miss/invalidate 都打 key=value log,供 KQL 撈 hit rate。
                            - 只服務 User OBO 讀路徑(短連線);Admin MI 寫路徑走
                              gatekeeper_publish,不共用此快取/連線邏輯。
VERSION: 1.6
2026.07.07 George : v1.6 — 修正 v_my_skills.description 永遠是空字串的 bug
                            - 新增 list_allowed_skills_full():不再信任 SQL
                              description 欄位,改從 Blob(source of truth)
                              撈 SKILL.md 內容解析完整 frontmatter,重用既有
                              _fetch_skill_content() cache
                            - 砍掉 v1.5 的 list_allowed_skills()(確認無其他
                              呼叫端使用),被 list_allowed_skills_full() 取代
                            - list_allowed_skills_full() 內加 logging.info
                              印出撈到的完整 skill 清單內容,方便 debug RLS /
                              frontmatter 解析問題(見下方 method 內註解)
VERSION: 1.5 (已移除該版新增的 method,見上方 v1.6)
2026.07.05 George : v1.5 — 新增 list_allowed_skills() public method,
                            重用 _fetch_allowed_skills 給 core_handler.list_skills()
                            當「純列清單」入口(不 materialize、不建 provider)。
                            動機:list_skills 當初沒考慮 Mode B,故意不新建
                            SQL view——v_my_skills + 既有 OBO 查詢已經是唯一
                            source of truth,避免兩處權限邏輯分裂維護。
2026.06.03 George : v1.4 — 新增 _TrackingSkillsProvider:覆寫 _load_skill,
                            記錄 model 實際 load 的 skill 到 .loaded_skills。
                            供 code_agent_hosted 取代舊的關鍵字比對
                            detect_skills_referenced()。所有 provider(含空)
                            都改用此子類,.loaded_skills 屬性恆存在。
                            _load_skill 覆寫採「簽名無關轉發」(*args/**kwargs
                            原樣轉回 super),已在 SDK 1.0.0rc3(現行 runtime)
                            驗證;同時相容 RC3→v1.0 migration 後的雙參數簽名,
                            升級不需改此檔。
2026.05.22 George : v1.0 初版 — Per-turn dynamic SkillsProvider
2026.05.22 George : v1.1 (廢棄) 曾加 fallback_provider 機制
2026.05.22 George : v1.2 — Binary flag 設計,移除 fallback_provider
                            - SQL/Blob 故障時統一 yield 空 provider
                            - env var rename: FABRIC_SQL_* → AZURE_SQL_*
                            - 對齊 OBO_SCOPE_REGISTRY 的 AZURE_SQL_ACCESS_TOKEN
                              (scope: https://database.windows.net/.default)
2026.05.25 George : v1.3 — Cache key 從 version 改為 updated_at
                            - 配合「Blob 覆蓋 latest、SQL 不留歷史」的策略
                            - SkillMetadata 拿掉 version,新增 updated_at
                            - v_my_skills view 對應改:回傳 updated_at(不回傳 version)
                            - TTL 1 小時保留作為保險,主要 invalidation 靠 updated_at 變更
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import struct
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import aioodbc
import yaml
from agent_framework import SkillsProvider
from azure.storage.blob.aio import BlobServiceClient
from cachetools import TTLCache

# 2026.08.14 George : v1.9 — skill 資料夾過濾規則的唯一真相。跟 blob 路徑
# 推導公式放在同一檔,避免 Mode A / Mode B 各自漂移。
from skills_sync import (
    SKILL_ENTRY_FILENAME,
    derive_resource_scan_args,
    select_skill_files,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================

# Azure SQL token authentication — Microsoft 規定的 ODBC SQL_COPT_SS_ACCESS_TOKEN
SQL_COPT_SS_ACCESS_TOKEN = 1256

# 2026.07.20 George : #3 — 診斷查詢 debug flag。_fetch_allowed_skills 每 turn 會
# 多打一次 SELECT SUSER_SNAME(), USER_NAME(), ORIGINAL_LOGIN() 純診斷 principal
# 對不對。正式環境預設關閉(省每 turn 一趟 round-trip);要查 RLS grant 對不對
# 時才設 SKILLS_RLS_DIAG_ENABLED=true 開。⚠️ 預設值必須是關閉。
_RLS_DIAG_ENABLED = os.environ.get("SKILLS_RLS_DIAG_ENABLED", "false").strip().lower() in (
    "1", "true", "yes", "on",
)

# RLS 清單快取 TTL(秒)—— 只是保底防背景漂移;主要失效靠 Mode B publish
# 主動 invalidate。預設 300s。
try:
    _SKILLS_RLS_CACHE_TTL = max(1, int(os.environ.get("SKILLS_RLS_CACHE_TTL", "300")))
except ValueError:
    _SKILLS_RLS_CACHE_TTL = 300


# ============================================================================
# Data Model
# ============================================================================
@dataclass(frozen=True)
class SkillMetadata:
    """對應 v_my_skills view 的 row"""
    skill_name: str
    owner_upn: str | None  # None = 全域技能;有值 = 該 UPN 的私有技能
    skill_key: str  # schema v2 PK(全域時等同 skill_name),cache key 用
    blob_path: str
    updated_at: str  # ISO format datetime string,作為 cache key
    # 2026.08.14 George : v1.9 — skill 資料夾 prefix(必以 '/' 結尾)。
    # schema v2.1 之前部署的 view 沒這個欄位,預設空字串代表「不列舉,
    # 退回只拿 SKILL.md」——未跑 migration 的環境不會因此壞掉。
    blob_prefix: str = ""
    # 2026.08.21 George : v1.12 — True = 不出現在對外 skill 目錄,只能由 parent
    # skill 指名載入。預設 False 讓未跑 v2.2 migration 的環境、以及直接
    # 建構 SkillMetadata 的測試維持原行為。
    is_internal: bool = False


# 2026.08.22 George : v1.13 — 判準是「自己宣告 children」(= 情境層 parent),
# 不是「被別人列為 child」;寫反會擋掉真正要執行的能力層技能。
# 2026.09.04 George : v1.14 — 抽出名單版供 scenario 收斂使用。順手對齊
# core_handler._declared_children() 與 _metadata_list():`children:` 寫成裸字串
# 時也算 parent(舊版回 False,跟另外兩處不一致)。
def _declared_child_names(content: str) -> list[str]:
    if not content.startswith("---"):
        return []
    parts = content.split("---", 2)
    if len(parts) < 3:
        return []
    try:
        fm = yaml.safe_load(parts[1])
    except Exception:
        # 解析不出來就當它不是 parent —— 寧可多載一個 skill,也不要因為
        # 一個 YAML 錯字讓能力層技能整個消失。
        return []
    if not isinstance(fm, dict):
        return []
    meta = fm.get("metadata")
    if not isinstance(meta, dict):
        return []
    children = meta.get("children")
    if isinstance(children, str):
        children = [children]
    if not isinstance(children, (list, tuple)):
        return []
    return [str(c) for c in children if c]


def _declares_children(content: str) -> bool:
    return bool(_declared_child_names(content))


# 2026.08.14 George : v1.8 — schema v2 下同一個 skill_name 可能同時存在全域版與
# 使用者私有版,v_my_skills 會回兩列且刻意不做 tie-break(見 schema v2 §8),
# 由這裡決定「私有優先於全域」。
def _dedupe_private_over_global(metas: list[SkillMetadata]) -> list[SkillMetadata]:
    picked: dict[str, SkillMetadata] = {}
    for m in metas:
        current = picked.get(m.skill_name)
        if current is None or (current.owner_upn is None and m.owner_upn is not None):
            picked[m.skill_name] = m

    if len(picked) != len(metas):
        logger.info(
            "[Skills] dedupe: %d row(s) → %d skill(s) (private overrides global)",
            len(metas), len(picked),
        )
    return list(picked.values())


# ============================================================================
# Tracking SkillsProvider
# 2026.06.03 George : v1.4 — referenced 改用「實際 load_skill」為真相來源
# ----------------------------------------------------------------------------
# 背景:code_agent_hosted 舊版用 detect_skills_referenced() 對 CodingAgent 的
# 回應「文字」做關鍵字計數(命中 >=2 個 keyword 即算 referenced)。那量的是
# 「文字提及」而非「實際載入」,所以會同時誤報(model 文字提到某 skill 卻沒
# load)與漏報(真的 load 了卻沒湊滿 keyword)。實務上看到的矛盾 log
# (`Loading skill: html-ppt` 但 `Round 0 referenced: [...]` 完全沒有 html-ppt)
# 就是這個落差。
#
# MAF SkillsProvider 把 load_skill tool 建成:
#     func=lambda skill_name: self._load_skill(skills, skill_name)
# lambda 捕捉的是 self,因此覆寫子類的 _load_skill 就能攔到「每一次真正的
# 載入」—— 而 _load_skill 正是發出 `Loading skill: %s` log 的同一個方法。
# 於是 loaded_skills 與 MAF 實際載入逐一對應,成為 referenced 的可信來源。
#
# 所有 provider(含空 provider)都用這個子類,故 .loaded_skills 屬性恆存在,
# 下游可無條件 getattr 讀取。
# ============================================================================
# 2026.08.15 George : v1.11 — resource_name 前綴容錯(Mode A / Mode B 共用)
# ----------------------------------------------------------------------------
# MAF 註冊 file skill 的 resource 時,name 是「相對 skill 根目錄的完整路徑」
# (_discover_resource_files 的 rel_path),而 FileSkill.get_resource() 是完全
# 比對、沒有 basename fallback → 作者在 SKILL.md 寫 `style.css` 而檔案在
# assets/ 底下時,一律回 "Error: Resource '...' not found"。
#
# 不把這件事丟回給 skill 作者:平台無法保證每個開發者都寫同一種前綴風格,
# 而寫錯的懲罰是靜默降級(模型改用 open() 繞路,產出看似成功但沒套到樣式)。
# 這裡改成在讀取端解析名稱 —— 查不到就用同一個 basename 去試各個已知目錄,
# 兩種寫法都能命中。候選目錄沿用 derive_resource_scan_args() 的推導結果,
# 不另外列舉,維持「目錄清單只有一個來源」。
#
# 只用 public API(get_resource / _find_skill),不碰 FileSkill._resources。
class PrefixTolerantSkillsProvider(SkillsProvider):
    """SkillsProvider 子類:read_skill_resource 的 resource_name 前綴容錯。"""

    # 由 from_paths 呼叫端在建構後指派(derive_resource_scan_args 的第二個回傳值)
    resource_directories: tuple[str, ...] = ()

    async def _read_skill_resource(self, skills, skill_name, resource_name, **kwargs):  # type: ignore[override]
        resolved = await self._resolve_resource_name(skills, skill_name, resource_name)
        return await super()._read_skill_resource(skills, skill_name, resolved, **kwargs)

    async def _resolve_resource_name(self, skills, skill_name: str, resource_name: str) -> str:
        """回傳實際註冊的 resource name;解析不出來時原樣回傳,讓 MAF 產生原本的錯誤。"""
        if not skill_name or not resource_name or not resource_name.strip():
            return resource_name

        skill = self._find_skill(skills, skill_name)
        if skill is None or await skill.get_resource(resource_name) is not None:
            return resource_name

        basename = resource_name.replace("\\", "/").rsplit("/", 1)[-1]
        candidates: list[str] = []
        if basename != resource_name:
            candidates.append(basename)
        candidates += [
            f"{d}/{basename}" for d in self.resource_directories if d not in (".", "")
        ]

        matches: list[str] = []
        for candidate in candidates:
            if candidate == resource_name or candidate in matches:
                continue
            if await skill.get_resource(candidate) is not None:
                matches.append(candidate)

        if not matches:
            return resource_name

        if len(matches) > 1:
            logger.warning(
                "[Skills] resource_name '%s' is ambiguous in skill '%s': %s — using '%s'",
                resource_name, skill_name, matches, matches[0],
            )
        else:
            logger.info(
                "[Skills] resource_name '%s' resolved to '%s' (skill=%s)",
                resource_name, matches[0], skill_name,
            )
        return matches[0]


class _TrackingSkillsProvider(PrefixTolerantSkillsProvider):
    """SkillsProvider 子類:記錄 model 實際 load_skill / read_skill_resource 的目標。"""

    # 2026.08.31 George : route_only — 由 code_agent_hosted.run() 在進 turn loop 前
    # 指派。provider 由 factory 建立,拿不到 ConversationState,只能用屬性傳遞。
    route_only: bool = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # 依載入順序、去重後保留;由 code_agent_hosted 讀取設給 state.skills_referenced
        self.loaded_skills: list[str] = []
        # 2026.08.31 George : route_only — 記錄實際讀取的 resource,(skill, resource)。
        # 路由品質評估需要知道模型除了選對 skill,有沒有照 SKILL.md 的指示把附帶
        # 資源也讀進去 —— 只看 loaded_skills 看不出來。
        self.loaded_resources: list[tuple[str, str]] = []

    async def _load_skill(self, skills, skill_name):  # type: ignore[override]
        # 2026.06.07 George : GA 1.8.0 遷移(語義式) — parent _load_skill 已改為
        # async 雙參數 `(self, skills, skill_name) -> str`,load_skill tool 內部以
        # `await self._load_skill(skills, skill_name)` dispatch 到本 override。
        # ⚠ 舊版 sync `*args` 寫法在 1.8.0 下會 silently 失效(非 TypeError):
        #   super()._load_skill(...) 回傳的是「未 await 的 coroutine」,isinstance
        #   檢查為 False → loaded_skills 永遠空(skill 仍會載,但 referenced 追蹤斷掉)。
        #   已用端到端測試驗證:sync→async 是必要修正,*args 轉發救不了。
        # 行為等價:成功載入(回 str 且非 "Error:")才記入 loaded_skills,順序去重不變。
        result = await super()._load_skill(skills, skill_name)
        if (
            isinstance(result, str)
            and not result.startswith("Error:")
            and skill_name
            and skill_name not in self.loaded_skills
        ):
            self.loaded_skills.append(skill_name)
        return result

    async def _read_skill_resource(self, skills, skill_name, resource_name, **kwargs):  # type: ignore[override]
        # 2026.08.31 George : route_only — 記錄實際讀到的 resource。
        # 失敗判斷對齊上面的 _load_skill:MAF 回傳 "Error: ..." 字串而不拋例外,
        # 所以不能用 try/except 偵測。記的是呼叫端要求的名字(而非父類前綴容錯後的
        # 實際名字),因為要評估的是模型的行為。
        result = await super()._read_skill_resource(
            skills, skill_name, resource_name, **kwargs
        )
        # ≠ _load_skill:MAF 的 _read_skill_resource 回傳型別是 Any(callable resource
        # 可能回 dict/bytes),所以判「不是 Error 字串」而非「是成功的 str」。
        failed = isinstance(result, str) and result.startswith("Error:")
        entry = (skill_name, resource_name)
        if (
            not failed
            and skill_name
            and resource_name
            and entry not in self.loaded_resources
        ):
            self.loaded_resources.append(entry)
        return result

    async def _run_skill_script(self, skills, skill_name, script_name, args=None, **kwargs):  # type: ignore[override]
        # 2026.08.31 George : route_only 保險絲 #4
        # MAF (_skills.py) 無條件註冊 run_skill_script,且 require_script_approval
        # 預設 False → approval_mode="never_require"。也就是說模型可以在 _call_agent
        # 內部、也就是 turn loop 短路點「之前」就把腳本跑掉 —— 這是唯一一條繞過
        # 短路點的副作用路徑。
        # 現行實質防護是 skills_sync 同步時排除 scripts/ 目錄(檔案不落地),但那是
        # 內容層約定,會隨 skill 自動產生而漂移。這裡補成結構層。
        if self.route_only:
            raise RuntimeError(
                f"[route_only] blocked run_skill_script(skill={skill_name!r}, "
                f"script={script_name!r}) — scripts must not execute in "
                "routing-only mode."
            )
        return await super()._run_skill_script(
            skills, skill_name, script_name, args, **kwargs
        )


# ============================================================================
# SkillsProviderFactory
# ============================================================================
class SkillsProviderFactory:
    """Build per-turn, user-scoped SkillsProvider."""

    # Skill content cache — 跨 user 共享(content 跟 user 無關,只跟 updated_at 有關)
    # 主要 invalidation:cache key 含 updated_at,Gatekeeper publish 後 updated_at
    #                    變更 → 自然 cache miss
    # 保險 TTL 1 小時:萬一 publish 路徑漏更新 SQL,cache 也不會無限保留舊內容
    _content_cache: TTLCache = TTLCache(maxsize=200, ttl=3600)

    # 2026.08.14 George : v1.9 — skill 資料夾檔案清單快取。跟 _content_cache 用
    # 同一組 key(skill_key, updated_at):Gatekeeper publish 後 updated_at 變動
    # → 清單與內容一起失效,不會出現「新內容配舊檔清單」的短暫不一致。
    # 快取的是【已經過濾】的結果,不是原始列舉 —— 上限截斷的判斷隨之固定,
    # 同一版本不會兩輪拿到不同的檔案集合。
    _listing_cache: TTLCache = TTLCache(maxsize=200, ttl=3600)

    # ------------------------------------------------------------------
    # RLS 清單快取(#3)—— per-principal「這個人現在能看到哪些 skill」
    # ⭐ 資安關鍵:key = OBO token 的 sha256(唯一識別 token 背後的 principal),
    #    絕不能用 session_id / 連線字串 —— 否則 A 的清單會被 B 複用 = 權限外洩。
    #    跟 #2 obo_helper 用 assertion hash 是同一套思路。
    # 過期策略雙軌:
    #    (a) 短 TTL(_SKILLS_RLS_CACHE_TTL,預設 300s)—— 保底防背景漂移。
    #    (b) Mode B publish(approve_pending_skill)主動 invalidate_rls_cache()
    #        —— 發佈後使用者下一 turn 立刻看到新 skill,不用等 TTL。
    # 只服務 User OBO 讀路徑(短連線)。Admin MI 寫路徑(繞 RLS 寫 grants)走
    # gatekeeper_publish,不共用此快取/連線邏輯。
    # ------------------------------------------------------------------
    _rls_cache: TTLCache = TTLCache(maxsize=512, ttl=_SKILLS_RLS_CACHE_TTL)
    # 僅供 log 分類:miss 是 cold(沒看過)還是 refresh(TTL 過期 / 被 invalidate)
    _rls_seen_principals: set = set()

    def __init__(
        self,
        blob_client: BlobServiceClient,
        blob_container: str = "skills",
        base_dir: Path | None = None,
        sql_server: str | None = None,
        sql_database: str | None = None,
    ) -> None:
        """
        Args:
            blob_client: 已認證的 BlobServiceClient(用 Managed Identity)
            blob_container: SKILL.md 存放的 container name
            base_dir: materialize 根目錄,預設 SKILLS_MATERIALIZE_BASE
                     or /tmp/openclaw_skills
            sql_server: Azure SQL endpoint,預設讀 AZURE_SQL_SERVER
            sql_database: 資料庫名,預設讀 AZURE_SQL_DATABASE
        """
        self._blob_client = blob_client
        self._container = blob_container
        self._base_dir = base_dir or Path(
            os.environ.get("SKILLS_MATERIALIZE_BASE", "/tmp/openclaw_skills")
        )
        self._sql_server = sql_server or os.environ.get("AZURE_SQL_SERVER", "")
        self._sql_database = sql_database or os.environ.get("AZURE_SQL_DATABASE", "")

        if not self._sql_server or not self._sql_database:
            raise ValueError(
                "AZURE_SQL_SERVER and AZURE_SQL_DATABASE must be set "
                "(via env var or constructor args)"
            )

    # ------------------------------------------------------------------
    # RLS 快取 helper(#3)
    # ------------------------------------------------------------------
    @staticmethod
    def _principal_key(sql_token: str) -> str:
        """RLS 快取 key = OBO token 的 sha256(唯一識別 principal,不存原始 token)。"""
        return hashlib.sha256(sql_token.encode("utf-8")).hexdigest()

    @classmethod
    def invalidate_rls_cache(cls, reason: str = "manual") -> int:
        """清掉 RLS 清單快取(Mode B publish 後主動失效用)。

        發佈時無法得知哪些 principal 受影響(grant 關係在 SQL 端),故一律清全部
        —— 寧可多幾次 cache miss,也不要讓任何人看到過期的授權清單。回傳清掉的條目數。
        """
        n = len(cls._rls_cache)
        cls._rls_cache.clear()
        logger.info(
            "[Skills] rls_cache event=invalidate reason=%s cleared=%d", reason, n,
        )
        return n

    # ------------------------------------------------------------------
    # Step 1: 用 OBO token 連 Azure SQL,查 v_my_skills(RLS 自動過濾)
    # ------------------------------------------------------------------
    async def _fetch_allowed_skills(self, sql_token: str) -> list[SkillMetadata]:
        """
        用 user OBO Azure SQL token 連線,查 v_my_skills view。
        RLS 自動把結果過濾成「只有當前 UPN 有權限的 skill」。

        2026.07.20 George : #3 — per-principal 快取(key = sha256(sql_token))。
        命中直接回,不開連線;未命中才短連線查 SQL 並寫快取。
        """
        principal = self._principal_key(sql_token)

        cached = self._rls_cache.get(principal)
        if cached is not None:
            logger.info(
                "[Skills] rls_cache event=hit principal=%s skills=%d",
                principal[:8], len(cached),
            )
            return list(cached)

        cause = "refresh" if principal in self._rls_seen_principals else "cold"
        logger.info(
            "[Skills] rls_cache event=miss principal=%s cause=%s ttl=%ds",
            principal[:8], cause, _SKILLS_RLS_CACHE_TTL,
        )

        # Microsoft ODBC token 格式:長度 prefix + UTF-16-LE bytes
        token_bytes = sql_token.encode("utf-16-le")
        token_struct = struct.pack(
            f"=i{len(token_bytes)}s", len(token_bytes), token_bytes
        )

        # Linux ACA container 上的 driver name
        conn_str = (
            f"Driver={{ODBC Driver 18 for SQL Server}};"
            f"Server={self._sql_server},1433;"
            f"Database={self._sql_database};"
            f"Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        )

        async with aioodbc.connect(
            dsn=conn_str,
            attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct},
        ) as conn:
            async with conn.cursor() as cursor:
                # 2026.06.09 George: RLS 診斷
                # v_my_skills 依「連線進來的 principal」過濾。互動路徑是 user
                # OBO(SUSER_SNAME = user UPN);routine 路徑是 ACA MI(token auth)。
                # 若回 0 列,通常不是連線/auth 失敗,而是這個 principal 在
                # user_skill_grants 沒有對應列 → grant 對象就是下面印出來的名字。
                # 一次印三個函式,因為 token 連線下 SUSER_SNAME / USER_NAME /
                # ORIGINAL_LOGIN 可能不同,要對齊 v_my_skills 實際比對的那一個。
                # 診斷失敗不阻斷主查詢。
                # 2026.07.20 George : #3 — 每 turn 多一趟純診斷 round-trip,改由
                # SKILLS_RLS_DIAG_ENABLED gate(正式環境預設關閉)。
                if _RLS_DIAG_ENABLED:
                    try:
                        await cursor.execute(
                            "SELECT SUSER_SNAME(), USER_NAME(), ORIGINAL_LOGIN()"
                        )
                        diag = await cursor.fetchone()
                        if diag is not None:
                            logger.info(
                                "[Skills] Connected principal — SUSER_SNAME=%r, "
                                "USER_NAME=%r, ORIGINAL_LOGIN=%r",
                                diag[0], diag[1], diag[2],
                            )
                    except Exception as e:
                        logger.warning(
                            "[Skills] Principal diagnostic failed (continuing): "
                            "%s: %s", type(e).__name__, e,
                        )

                await cursor.execute(
                    "SELECT skill_name, owner_upn, skill_key, blob_path, updated_at, "
                    "blob_prefix, is_internal "
                    "FROM v_my_skills"
                )
                rows = await cursor.fetchall()

        # pyodbc Row 支援以欄位名做屬性存取,故 SELECT 順序異動不影響下面的對應。
        # 2026.08.21 George : v1.12 — 這裡帶出 is_internal 但不過濾。本方法同時
        # 服務 runtime materialize 路徑,在此濾掉 internal 等同於讓 parent 指名
        # 的 child 永遠載不進來。
        metas = _dedupe_private_over_global([
            SkillMetadata(
                skill_name=r.skill_name,
                owner_upn=r.owner_upn,
                skill_key=r.skill_key,
                blob_path=r.blob_path,
                updated_at=r.updated_at.isoformat() if r.updated_at is not None else "",
                blob_prefix=r.blob_prefix or "",
                is_internal=bool(r.is_internal),
            )
            for r in rows
        ])

        # 2026.07.20 George : #3 — 寫入 per-principal RLS 快取(含空結果:同一 TTL
        # 內不重查;Mode B publish 會主動 invalidate)。principal 記入 seen set
        # 供下次 miss 分類 cold / refresh。
        self._rls_cache[principal] = metas
        self._rls_seen_principals.add(principal)
        if len(self._rls_seen_principals) > 2048:
            self._rls_seen_principals.clear()

        if metas:
            logger.info(
                "[Skills] Fetched %d allowed skills via Azure SQL RLS (principal=%s)",
                len(metas), principal[:8],
            )
        else:
            # 2026.08.14 George : schema v2 起 v_my_skills 是 LEFT JOIN + is_public,
            # 所以 0 列代表「沒有 grant」且「一個公開技能都沒有」,不再等同於前者。
            logger.warning(
                "[Skills] Fetched 0 allowed skills via Azure SQL RLS — "
                "connection/auth OK but v_my_skills returned no rows. "
                "Either the principal has no grants, or no skill is marked "
                "is_public=1 (enable SKILLS_RLS_DIAG_ENABLED to log the "
                "principal name the view filters on).",
            )
        return list(metas)

    # ------------------------------------------------------------------
    # 2026.07.07 George : 砍掉 list_allowed_skills()(v1.5 加的舊版、只回
    # SkillMetadata 四欄位的 public wrapper)。確認沒有其他呼叫端在用後移除
    # —— 功能已被 list_allowed_skills_full() 取代,後者才會正確帶出
    # description(前者的 SQL description 欄位從寫入路徑就沒填值,見
    # list_allowed_skills_full() 上方註解)。
    # create_provider_scope() 走的是 _fetch_allowed_skills(),不受影響。
    # ------------------------------------------------------------------

    # Public wrapper: 完整 frontmatter 版
    # 2026.07.07 George : 修正 bug —— v_my_skills.description 從寫入路徑
    # (gatekeeper_publish.py publish 進 skills table 那段)就沒有真的被
    # 填值,導致這欄查出來永遠是空字串。
    #
    # 沒有選擇回頭補 SQL 寫路徑(補一份 description 欄位),因為那樣會
    # 產生「SQL 一份 description、Blob SKILL.md 一份」的雙來源,只要哪次
    # publish 漏更新 SQL,兩邊就會 drift。這裡的架構本來就是 Blob 為
    # source of truth(SQL 只存 skill_name/blob_path/updated_at 供 RLS
    # 過濾用),所以 list_skills 需要的 description(以及其他 frontmatter
    # 欄位)直接從 Blob 撈,永遠跟實際內容一致。
    #
    # 2026.08.14 George : schema v2 已直接拿掉 skills.description 欄位,上述
    # 「Blob 為唯一來源」從約定變成 schema 強制。
    #
    # 重用 _fetch_skill_content() 既有的 TTL cache(跟 create_provider_scope
    # 共用),不會為了列清單多打一次 Blob。
    # ------------------------------------------------------------------
    async def list_allowed_skills_full(self, sql_token: str) -> list[dict]:
        """
        回傳目前使用者被授權的 skill 完整 frontmatter,而非只有 SQL
        view 的 skill_name/blob_path/updated_at 等少數欄位。

        做法:先查 v_my_skills 拿到 allowed skill 清單(RLS 過濾 +
        blob_path),再對每個 skill 用既有 _fetch_skill_content() 撈
        SKILL.md 內容並解析 frontmatter YAML。

        Returns:
            list[dict] —— 每個 dict 是該 skill SKILL.md frontmatter 的
            完整內容(至少含 "name"),另外帶 "_blob_path" / "_updated_at"
            供除錯用(呼叫端顯示時可以選擇濾掉底線開頭的 key)。
            單一 skill 讀取或解析失敗不影響其他 skill(該筆的
            description 會標記錯誤原因)。

        Raises:
            由呼叫端決定如何處理 SQL 查詢失敗(跟 list_allowed_skills
            行為一致,這裡不吞例外)。
        """
        metas = await self._fetch_allowed_skills(sql_token)
        if not metas:
            return []

        async def _load_one(meta: SkillMetadata) -> dict:
            try:
                content = await self._fetch_skill_content(meta)
            except Exception as e:
                logger.exception(
                    "[Skills] list_allowed_skills_full: blob fetch failed "
                    "for %s: %s", meta.skill_name, e,
                )
                return {
                    "name": meta.skill_name,
                    "description": "(讀取 SKILL.md 失敗,見 log)",
                    "_blob_path": meta.blob_path,
                    "_updated_at": meta.updated_at,
                    "_is_internal": meta.is_internal,
                    "_files": [SKILL_ENTRY_FILENAME],
                }

            fm: dict = {}
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    try:
                        parsed = yaml.safe_load(parts[1])
                        if isinstance(parsed, dict):
                            fm = parsed
                    except Exception as e:
                        logger.warning(
                            "[Skills] list_allowed_skills_full: frontmatter "
                            "parse failed for %s: %s", meta.skill_name, e,
                        )
                        fm = {"description": "(frontmatter 解析失敗)"}

            fm.setdefault("name", meta.skill_name)
            fm["_blob_path"] = meta.blob_path
            fm["_updated_at"] = meta.updated_at
            # 2026.08.21 George : v1.12 — 底線前綴是刻意的:_format_skills_catalog
            # 會跳過底線開頭的 key,所以這個拓樸旗標不會被印給宿主。
            fm["_is_internal"] = meta.is_internal
            try:
                listed_files = await self._list_skill_files(meta)
            except Exception as e:
                logger.warning(
                    "[Skills] list_allowed_skills_full: folder listing failed "
                    "for %s, falling back to %s: %s",
                    meta.skill_name, SKILL_ENTRY_FILENAME, e,
                )
                listed_files = []
            fm["_files"] = [
                SKILL_ENTRY_FILENAME,
                *(path for path in listed_files if path != SKILL_ENTRY_FILENAME),
            ]
            return fm

        results = await asyncio.gather(*[_load_one(m) for m in metas])

        # 2026.07.07 George : debug logging —— 印出每個 skill 撈到的 frontmatter
        # dict,方便對照 RLS 授權範圍是否正確、frontmatter 有沒有解析出預期欄位
        # (之前只回 SkillMetadata 四欄位時很難確認 description 到底是 SQL 的空值
        # 還是 Blob 內容真的沒寫 description)。
        #
        # 差別:skill 內文(description 等 frontmatter 字串)可能很長,直接整包丟
        # 進 log 會洗版,所以只留「前 50 + 後 50 字元」,中間用長度標記帶過。
        # 用 %r 而非 f-string,避免內容裡有奇怪字元時 log 直接炸掉。
        #
        # 2026.07.07 George : 改遞迴版 —— 原本只處理最外層 key 的字串值,
        # 像 metadata.call_shape / metadata.input_contract 這種包在 nested
        # dict 裡的長字串完全沒被截斷(實測 call_shape 865 字元、
        # input_contract 1207 字元,整包印出來比 description 還洗版)。
        # 改成對 dict 遞迴進每個 value、對 list 遞迴進每個 element,字串才會
        # 不管藏在第幾層都被截斷。
        def _elide(value, head: int = 50, tail: int = 50):
            """字串太長時只保留前 head / 後 tail 字元;dict/list 遞迴處理。"""
            if isinstance(value, str) and len(value) > head + tail:
                return f"{value[:head]}…[{len(value)} chars]…{value[-tail:]}"
            if isinstance(value, dict):
                return {k: _elide(v, head, tail) for k, v in value.items()}
            if isinstance(value, list):
                return [_elide(v, head, tail) for v in value]
            return value

        log_results = [
            {k: _elide(v) for k, v in fm.items()}
            for fm in results
        ]
        logger.info(
            "[Skills] list_allowed_skills_full: %d skill(s) resolved: %r",
            len(results), log_results,
        )

        return results

    # ------------------------------------------------------------------
    # 2026.08.21 George : v1.12 — 單一 skill 的 SKILL.md 正文
    #
    # 動機:對外只有 list_skills 一個發現表面,而它只回 frontmatter
    # (_format_skills_catalog 不輸出正文)。情境層 parent skill 的價值就在正文
    # ——分支、失敗態樣、recovery path 全在那裡,而且刻意寫得很長。沒有這個
    # 方法,那些內容就只能塞回 frontmatter 常駐,progressive disclosure 不成立。
    # ------------------------------------------------------------------
    async def get_skill_content(
        self, sql_token: str, skill_name: str
    ) -> str | None:
        """
        回傳單一 skill 的 SKILL.md 完整內容(含 frontmatter)。

        Args:
            sql_token: OBO exchange 後的 Azure SQL access token
            skill_name: skill 名稱(非 skill_key;全域/私有同名時取私有版,
                        與 materialize 路徑的 _dedupe_private_over_global 一致)

        Returns:
            SKILL.md 內容字串;該使用者無此 skill 權限或名稱不存在時回 None。
            兩者刻意不區分 —— 對呼叫端而言都是「拿不到」,分開講等於洩漏
            「系統裡存在這個 skill 但你沒權限」。

        ⭐ 刻意「不」套 is_internal 過濾:
            parent skill 的正文會指名 internal child。這裡若比照 list_skills
            擋掉 internal,就會複製出「parent 叫我去拿、但拿不到」的死路 ——
            跟在 SQL 端過濾是同一個錯誤,只是換一層。

        ⭐ 刻意「不」檢查「是否已先載入 parent」:
            那需要在 stateless 的 MCP 上維護「parent 是否已被 fetch」的狀態,
            而 child 內部的 Step 0 / [NEEDS_INFO] 已經擋在真正的寫入之前。
            繞過 parent 直接 fetch child 不會造成安靜的破口,只會多一輪
            round trip。為了一個已被接住的情境去引入狀態,不划算。

        權限邊界完全沿用 _fetch_allowed_skills()(RLS + per-principal 快取),
        不另外開一條查詢路徑。
        """
        metas = await self._fetch_allowed_skills(sql_token)
        target = next((m for m in metas if m.skill_name == skill_name), None)
        if target is None:
            logger.info(
                "[Skills] get_skill_content: %r not in allowed set "
                "(%d skill(s) visible to this principal)",
                skill_name, len(metas),
            )
            return None

        content = await self._fetch_skill_content(target)
        logger.info(
            "[Skills] get_skill_content: %s resolved (%d chars, is_internal=%s)",
            skill_name, len(content), target.is_internal,
        )
        return content

    # ------------------------------------------------------------------
    # Step 2: 從 Blob 撈 skill content(有 in-process cache)
    # ------------------------------------------------------------------
    async def _fetch_skill_content(self, meta: SkillMetadata) -> str:
        # Cache key 用 updated_at:Gatekeeper publish 後 SQL updated_at 會變,
        # 自然 cache miss → 撈到新版內容
        # 2026.08.14 George : 前半段必須是 skill_key 而非 skill_name —— 本 cache 跨 user
        # 共享,schema v2 下同名的全域版與私有版會撞 key,等同內容跨使用者外洩。
        cache_key = (meta.skill_key, meta.updated_at)
        cached = self._content_cache.get(cache_key)
        if cached is not None:
            logger.debug(
                "[Skills] Cache hit: %s (updated_at=%s)",
                meta.skill_name, meta.updated_at,
            )
            return cached

        logger.info(
            "[Skills] Cache miss, fetching from Blob: %s (updated_at=%s, path=%s)",
            meta.skill_name, meta.updated_at, meta.blob_path,
        )
        blob_client = self._blob_client.get_blob_client(
            container=self._container,
            blob=meta.blob_path,
        )
        downloader = await blob_client.download_blob()
        content_bytes = await downloader.readall()
        content = content_bytes.decode("utf-8")
        self._content_cache[cache_key] = content
        return content

    # ------------------------------------------------------------------
    # Step 2.5: 列舉 skill 資料夾內容(有 in-process cache)
    # 2026.08.14 George : v1.9 新增
    # ------------------------------------------------------------------
    async def _list_skill_files(self, meta: SkillMetadata) -> list[str]:
        """
        回傳該 skill 應 materialize 的檔案清單(skill 資料夾內的相對路徑)。

        blob_prefix 為空(schema 尚未跑 v2.1 migration)時回傳空清單,呼叫端
        會退回「只寫 SKILL.md」的舊行為。這是刻意的:schema 落後於程式碼時
        降級,而不是整個 Mode B 掛掉。
        """
        if not meta.blob_prefix:
            return []

        cache_key = (meta.skill_key, meta.updated_at)
        cached = self._listing_cache.get(cache_key)
        if cached is not None:
            return cached

        container_client = self._blob_client.get_container_client(self._container)
        entries: list[tuple[str, int]] = []
        async for blob in container_client.list_blobs(
            name_starts_with=meta.blob_prefix
        ):
            if blob.name.endswith("/"):
                continue
            rel = blob.name[len(meta.blob_prefix):]
            if rel:
                entries.append((rel, blob.size or 0))

        selected = select_skill_files(meta.skill_name, entries)
        self._listing_cache[cache_key] = selected

        logger.info(
            "[Skills] Listed %s: %d blob(s) → %d file(s) selected (prefix=%s)",
            meta.skill_name, len(entries), len(selected), meta.blob_prefix,
        )
        return selected

    # ------------------------------------------------------------------
    # Step 3: Materialize 到 per-turn 目錄
    # ------------------------------------------------------------------
    async def _materialize_skills(
        self,
        metas: list[SkillMetadata],
        target_dir: Path,
        scenario: str = "",
    ) -> None:
        """
        並行下載所有 skill 的資料夾內容,寫到 target_dir/{skill_name}/ 底下。

        目錄結構對齊 SkillsProvider 期待:
            target_dir/
              ms-graph-calendar/SKILL.md
              ms-graph-calendar/references/graph-api.md
              fabric-sql-tasks/SKILL.md

        2026.08.14 George : v1.9 — 從「每個 skill 只下載 blob_path 一個檔」改為
        「列舉 blob_prefix 還原整個資料夾」。SKILL.md 仍走 _fetch_skill_content()
        以沿用既有的內容 cache;其餘檔案(references/、assets/)按需下載、不進
        內容 cache —— 它們單檔可能是 MB 等級的二進位資產,塞進那個 maxsize=200
        的共享 cache 會把 SKILL.md 全數擠出去。

        2026.08.22 George : v1.13 — 宣告了 metadata.children 的情境層 skill 不落地。
        它描述的是宿主該做的事(讀行事曆、多輪接續),後端沒有那些能力;留在
        <available_skills> 裡只會跟同領域的 child 搶關鍵字。能力層 child 自己
        沒有 children,不受影響。

        2026.09.04 George : v1.14 — scenario 白名單收斂。v1.13 只藏 parent,沒藏
        「別的情境的 child」。宿主選定情境後,這裡只落地該情境 children 列到的
        skill。重要性在 N>1:同情境 N 個 sibling 彼此高度相似,而後端只有
        使用者原話 + 扁平 skill 池兩個訊號(parent 的編排指引不落地、credentials
        key 名也不進 prompt),不收斂就沒有任何依據分辨該用哪一個。

        ⚠️ 白名單而非「只濾 internal」:parent 沒指名的通用 skill 也不落地。
        選這個是因為實際上 `children` 本來就是指名宣告,而後端自行拉進一支
        parent 沒提過的 skill,正是兩層架構要壓制的不受控行為。代價:漏列會
        fail-closed 且對使用者靜默,線索只有底下那行 skipped log。

        無情境的輪次(scenario 空)不受影響,整池照舊 —— 通用 skill 仍可經由
        宿主不帶 scenario 的那些 turn 使用。
        全程 fail-open:scenario 空 / 查無此 skill / 它不是 parent → 維持全載。
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        scope = await self._resolve_scenario_scope(metas, scenario)

        async def write_one(meta: SkillMetadata) -> bool:
            if scope is not None and meta.skill_name not in scope:
                logger.info(
                    "[Skills] %s: outside scenario %r — skipped",
                    meta.skill_name, scenario,
                )
                return False

            # SKILL.md 一定要有,且必須先撈 —— 缺它整個 skill 就不成立,
            # 而且要先看過 frontmatter 才知道該不該落地。
            content = await self._fetch_skill_content(meta)
            if _declares_children(content):
                logger.info(
                    "[Skills] %s declares children — scenario skill, "
                    "not materialized for the internal agent",
                    meta.skill_name,
                )
                return False

            skill_dir = target_dir / meta.skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_root = skill_dir.resolve()

            await asyncio.to_thread(
                (skill_dir / SKILL_ENTRY_FILENAME).write_text,
                content,
                encoding="utf-8",
            )

            async def write_extra(rel: str) -> None:
                dest = skill_dir / rel
                # select_skill_files() 已擋過 path traversal,這裡是落地前的
                # 第二道:任何繞過字串檢查的形式,resolve() 後都會露出來。
                if not dest.resolve().is_relative_to(skill_root):
                    logger.warning(
                        "[Skills] %s: path escapes skill dir, skipped — %r",
                        meta.skill_name, rel,
                    )
                    return
                dest.parent.mkdir(parents=True, exist_ok=True)
                blob_client = self._blob_client.get_blob_client(
                    container=self._container,
                    blob=meta.blob_prefix + rel,
                )
                downloader = await blob_client.download_blob()
                await asyncio.to_thread(dest.write_bytes, await downloader.readall())

            extras = [
                r for r in await self._list_skill_files(meta)
                if r != SKILL_ENTRY_FILENAME
            ]
            if extras:
                await asyncio.gather(*[write_extra(r) for r in extras])
            return True

        written = 0
        if metas:
            written = sum(await asyncio.gather(*[write_one(m) for m in metas]))

        logger.info(
            "[Skills] Materialized %d/%d skills to %s (%d skipped)",
            written, len(metas), target_dir, len(metas) - written,
        )

    async def _resolve_scenario_scope(
        self, metas: list[SkillMetadata], scenario: str
    ) -> set[str] | None:
        """
        回傳該情境允許落地的 skill 名稱白名單;回 None = 不收斂(維持全載)。

        名單就是 parent 的 metadata.children 原封不動 —— 不額外放行通用 skill。
        三條 fail-open 路徑都只記 log 不拋錯 —— 宿主打錯字不該讓整個 turn 跑不了。
        注意此處只讀 Blob 拓樸,不碰 is_internal —— 未跑 v2.2 migration 的環境
        收斂一樣生效(v1.14 之前則否)。
        """
        if not scenario:
            return None

        target = next((m for m in metas if m.skill_name == scenario), None)
        if target is None:
            logger.warning(
                "[Skills] scenario %r not in this principal's allowed set "
                "— no scoping applied", scenario,
            )
            return None

        children = _declared_child_names(await self._fetch_skill_content(target))
        if not children:
            logger.warning(
                "[Skills] scenario %r declares no children (not a scenario skill?) "
                "— no scoping applied", scenario,
            )
            return None

        logger.info(
            "[Skills] scenario-scoped materialize: scenario=%s children=%s",
            scenario, sorted(children),
        )
        return set(children)

    # ------------------------------------------------------------------
    # Step 4: 主入口 — async context manager
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def create_provider_scope(
        self,
        sql_token: str,
        session_id: str,
        turn_id: str,
        scenario: str = "",
    ) -> AsyncIterator[SkillsProvider]:
        """
        為單一 user request 建立 SkillsProvider,scope 結束自動清理。

        Args:
            sql_token: OBO exchange 後的 Azure SQL access token
                      (從 state.user_data["AZURE_SQL_ACCESS_TOKEN"] 拿)
            session_id: state.session_id
            turn_id: 唯一識別子;建議用 state.job_id 或 uuid
            scenario: 宿主在 list_skills 選定的情境層 skill 名稱。空字串 = 不收斂;
                      帶了則只落地該 skill metadata.children 列到的(白名單)。

        Yields:
            SkillsProvider 物件,直接餵給 ChatAgent / Agent 的 context_providers

        Failure modes(全部統一 yield 空 provider,不 raise):
            - 沒拿到 sql_token         → 空 provider
            - SQL fetch 失敗            → 空 provider
            - Blob materialize 失敗     → 空 provider
            - User 沒有任何 grant       → 空 provider
            Turn 仍能跑完,LLM 看不到任何 advertised skill。
            Ops 處理:翻 DYNAMIC_SKILLS_ENABLED=false 重啟回 static 模式。
        """
        if not sql_token:
            logger.warning(
                "[Skills] No sql_token provided — yielding EMPTY provider "
                "(session=%s turn=%s)",
                session_id, turn_id,
            )
            yield _TrackingSkillsProvider(source=[])  # GA 1.8.0: SkillsProvider source-based,空 Skill 序列即空 provider
            return

        target_dir = self._base_dir / session_id / turn_id
        materialized = False

        try:
            # Step 1: 查 RLS-filtered skill list
            try:
                metas = await self._fetch_allowed_skills(sql_token)
            except Exception as e:
                logger.exception(
                    "[Skills] SQL fetch failed (session=%s turn=%s): %s "
                    "— yielding EMPTY provider. "
                    "Consider rolling back DYNAMIC_SKILLS_ENABLED=false.",
                    session_id, turn_id, e,
                )
                yield _TrackingSkillsProvider(source=[])  # GA 1.8.0: SkillsProvider source-based,空 Skill 序列即空 provider
                return

            if not metas:
                # 2026.08.14 George : schema v2 下這代表「沒 grant 且沒有任何公開技能」。
                logger.warning(
                    "[Skills] User has no visible skills "
                    "(session=%s turn=%s) — yielding EMPTY provider",
                    session_id, turn_id,
                )
                yield _TrackingSkillsProvider(source=[])  # GA 1.8.0: SkillsProvider source-based,空 Skill 序列即空 provider
                return

            # Step 2 & 3: 撈 content + materialize
            try:
                await self._materialize_skills(metas, target_dir, scenario)
                materialized = True
            except Exception as e:
                logger.exception(
                    "[Skills] Blob materialize failed (session=%s turn=%s): %s "
                    "— yielding EMPTY provider. "
                    "Consider rolling back DYNAMIC_SKILLS_ENABLED=false.",
                    session_id, turn_id, e,
                )
                yield _TrackingSkillsProvider(source=[])  # GA 1.8.0: SkillsProvider source-based,空 Skill 序列即空 provider
                return

            # Step 4: 建 file-based SkillsProvider
            # 2026.06.07 George : GA 1.8.0 遷移(語義式) — SkillsProvider 改為
            # source-based,file-based 載入改用 classmethod from_paths(內部包
            # FileSkillsSource)。from_paths 以 cls(...) 建構 → 保留 _TrackingSkillsProvider
            # 子類,loaded_skills 屬性與 _load_skill 追蹤照常運作。disable_caching
            # 預設 False — 不更動既有 caching 行為。(不轉 Filtering/Aggregating/
            # Deduplicating source-composition,留待架構升級 PR。)
            #
            # 2026.08.15 George : v1.10 — 兩個掃描參數由實際落地的檔案反推,
            # 使 MAF 自帶的副檔名 / 目錄白名單失效(詳見模組 docstring v1.10)。
            resource_exts, resource_dirs = await asyncio.to_thread(
                derive_resource_scan_args, target_dir
            )
            logger.info(
                "[Skills] Resource scan args derived: extensions=%r directories=%r "
                "(session=%s turn=%s)",
                resource_exts, resource_dirs, session_id, turn_id,
            )
            provider = _TrackingSkillsProvider.from_paths(
                str(target_dir),
                resource_extensions=resource_exts,
                resource_directories=resource_dirs,
            )
            # v1.11:同一組目錄再供 resource_name 前綴容錯使用
            provider.resource_directories = resource_dirs
            yield provider

        finally:
            # 只清理真的 materialize 過的目錄,避免不必要的 IO
            if materialized:
                try:
                    await asyncio.to_thread(
                        shutil.rmtree, target_dir, ignore_errors=True
                    )
                    logger.debug("[Skills] Cleaned up %s", target_dir)
                except Exception as e:
                    logger.warning(
                        "[Skills] Cleanup failed for %s: %s", target_dir, e,
                    )