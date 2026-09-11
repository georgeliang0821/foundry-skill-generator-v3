"""
Core Handler - 共用核心邏輯
====================================================================
從 main.py 抽離的 protocol-agnostic 核心,供多個 adapter 共用:
- main.py (Foundry Hosted Agent adapter)
- mcp_server.py (HTTP API adapter for Custom ACA)

包含:
- Workflow 初始化(startup)
- Workflow 執行(run_workflow)
- Session 管理(metadata persist via conversation_store)
- 累積式 turn history(跨輪 context 注入)
- Recent full outputs(N=1 sliding window,保留上一輪完整輸出)
- Debug 工具(skills listing, sync, clear)

VERSION: 1.11
2026.08.21 George : v1.11 — Skill Registry 分階(情境層 / 能力層)
- list_skills() 兩條 Mode B 路徑改為投影輸出:_project_scenario_skills()
  濾掉 is_internal=1 的能力層 skill。⭐ 只濾這一層 —— SQL(v_my_skills)與
  runtime materialize 都必須看得到 internal skill,否則 parent 正文指名的
  child 載不進來。Static 模式(Mode A)不套用,理由見該分支註解。
- 新增 fetch_skill(skill_name, credentials):取單一 skill 的 SKILL.md 正文。
  list_skills 只回 frontmatter,而情境層 parent 的編排指引寫在正文、且只有
  宿主(DA / Foundry helper)執行得了(例:查行事曆這種 EAA 無權限的動作),
  沒有這條路徑那些指引到不了執行者。
- _format_skills_catalog() 把 metadata.children 提到獨立一行,宿主靠它判斷
  「這是情境層,要先 fetch_skill」。
- 孤兒偵測 _detect_orphan_internal_skills():標了 internal 卻沒有任何 skill
  的 metadata.children 指向它 = 永久不可被發現(常見於 refine parent 時把
  children 寫掉)。admin bypass 路徑記 WARNING(完整目錄可信),user OBO
  路徑記 DEBUG(RLS 視野受限會誤報)。
- 相依:Azure SQL schema v2.2 migration(skills.is_internal)必須先跑。

VERSION: 1.10
2026.08.15 George : v1.10 補記 — 以下變更早已進 code,先前漏記 changelog
- 2026.05.22 Static SkillsProvider:DYNAMIC_SKILLS_ENABLED=false 時用
  _build_static_skills_provider() 把 SKILLS_DIR 內所有 skill 載給 coding_agent;
  sync_skills() 後會重建 provider 並 re-inject。
- 2026.06.01 MCP 邊界回傳投影 (_project_boundary_result):whitelist 投影,
  只作用於「同步回 MCP 邊界」的 result;bg task 寫進 Job Store 的仍是未投影
  的完整診斷包絡。
- 2026.06.12 max_replicas>1 (W2/W4/W5/W6/W7):
  - W2:check_pending_tasks 的 RUNNING 分支改查 Table Storage long-poll
    (CHECK_POLL_INTERVAL),取代 in-process asyncio.Event — job 跑在 instance A
    而 check 落在 instance B 時,B 的 event 永遠不會被 set。終態交付抽成
    _deliver_terminal_job()。
  - W4/W5:新增 _job_companion_loop(),與 bg task 並行輪詢 cancel flag +
    寫 heartbeat (CANCEL_POLL_INTERVAL / HEARTBEAT_INTERVAL)。
  - W5:orphan 惰性偵測 — check 查詢路徑順手判 heartbeat 逾時
    (ORPHAN_TIMEOUT_SEC) → 標 INTERRUPTED 當終態交付,不自動重跑。
  - W6:新增 shutdown_inflight_jobs(),SIGTERM 時用 _inflight_jobs 登記簿列舉
    本 replica 仍在跑的 job,標 interrupted 並砍 subprocess。
  - _deliver_result_or_notify 發 Teams 前回讀 result_picked_up 去重
    (跨 instance 已交付則跳過通知)。
- 2026.07.05~07.07 Dynamic Skills (Mode B) 身份分流:list_skills 改 async,
  user delegated token 走 OBO 查 v_my_skills (RLS 授權範圍);app/agent token
  只能借 ACA MI 的 admin bypass,回傳文字明確標註不是 RBAC 授權範圍。
- 2026.07.20~07.25 資源回收:shutdown() 關閉 job_store / conversation_store /
  _credential / _admin_credential / debug bundle client;_run_coding_agent_inner
  收尾寫入本輪 token 用量。
- 新增常數:CHECK_POLL_INTERVAL(2) / CANCEL_POLL_INTERVAL(5) /
  HEARTBEAT_INTERVAL(20) / ORPHAN_TIMEOUT_SEC(90)。
  ⚠️ 約束 CHECK_POLL_INTERVAL < GRACE_PERIOD_SECONDS,Teams 去重靠這個時序。
- 2026.08.15 移除 _deliver_result_or_notify 的 grace-recovered 分支 (死碼):
  register_waiter 只有 run_workflow 呼叫且在 finally 清掉,W2 後
  check_pending_tasks 不再註冊 waiter → detach 後第二次 has_waiter 恆為 False。
  grace sleep 保留,作用改為「給查表的 checker 標 picked_up 的時間」。

VERSION: 1.9
2026.05.19 George : v1.9 — P0 修 Adaptive Timeout 領取漏洞 (完整版)
- 問題:Adaptive Timeout detach 後背景任務跑完,Job Store status 已更新
  為 COMPLETED,使用者收到 Teams 通知回 thread 問結果時,check_pending_tasks
  用 find_running_by_session 找不到,誤回 no_running_task。
- 衍生問題 (v1.9 初版未涵蓋):「同步 long poll 領取」路徑沒標 picked_up,
  下一輪同 session retry 時新 RUNNING job 被舊 COMPLETED-未領取搶先撈到。
- 修法 (改 3 設計 — picked_up 標記責任歸 _deliver_result_or_notify):
  - 檔頭加 import json (v1.8 原本在 _run_coding_agent_inner 內 inline import,
    v1.9 終態分流在該 function 外用 json.loads,沒 inline 直接爆 NameError
    → 必須提升為 module-level import)
  - job_store.py: Job dataclass 加 result_picked_up 欄位 (預設 False)
  - job_store.py: 新增 find_recent_unpicked_by_session,涵蓋 RUNNING +
    COMPLETED/FAILED 且未領取 (24h 內)
  - 新增 _mark_result_picked_up helper: 統一 picked_up 標記寫入邏輯,
    失敗只 log warning 不阻斷主流程
  - _deliver_result_or_notify 加 session_id 參數,在 set_result 兩條路徑
    (sync + grace-recovered) 後呼叫 _mark_result_picked_up
  - cancelled 路徑的 has_waiter set_result 後也呼叫 _mark_result_picked_up
    (保險;雖然 find_recent_unpicked_by_session filter 不含 CANCELLED,撈
    不到這筆,但語意一致比較好維護)
  - check_pending_tasks 終態分流改用 _mark_result_picked_up (程式碼重用)
  - check_pending_tasks 改用 find_recent_unpicked_by_session 取代
    find_running_by_session
  - _run_coding_agent_inner 內兩個 _deliver_result_or_notify caller
    (正常完成、exception 收尾) 都傳 session_id
- 設計取捨:
  - Teams 通知路徑 (沒人在等) 不標 picked_up — 結果只在 Job Store,使用者
    下次 check_pending_tasks 走終態分流時才標記
  - _mark_result_picked_up 失敗只 warning 不 raise — 結果已交付給使用者,
    標記失敗最壞情況是下次同 session 撈到再領一次,使用者多看一次同樣結果
    不致命,比拋例外炸主流程好
- find_running_by_session 不動 (給 run_workflow §5.5 並行檢查用,語意需求
  不同 — 並行檢查只在意「真的還在跑」)
- Q1 決策:結果保留 1 天 (find_recent_unpicked_by_session max_age_hours=24)
- Q2 決策:領取一次後標記 picked_up,不支援連續取回 (UX 上不合理情境)
- Q3 決策:Adaptive Card 不加按鈕/不帶 session_id (使用者打字觸發即可)
- ⚠️ 已被 v1.10 (2026.06.12 W2) 部分取代:check_pending_tasks 的等待機制改為
  Table 查表 long-poll,本條描述的 in-memory waiter 交付語意現在只在
  run_workflow 的 80 秒同步窗內成立。

VERSION: 1.8
2026.05.18 George : v1.8 Phase 4 — Teams 通知 Logic App 整合
- 新增 jwt_helper.py (純 base64 decode JWT payload,不驗簽章)
- run_workflow 外殼:解析 __user_token 的 JWT claims (oid + upn),
  傳給 _job_store.create() 寫進 user_id / user_email 欄位
- job_store.py: Job dataclass 加 user_email 欄位、create() 簽章加
  user_email 參數
- _send_teams_notification_stub → _send_teams_notification:
  - 從 Job Store 拉 user identity (補篇 §3 唯一可靠來源,exception
    路徑下 state 可能未建立完整)
  - 組 webhook payload (job_id / session_id / user_email / user_oid /
    task_description / status / result_summary / error)
  - fire-and-forget asyncio.create_task 發送,不阻塞 bg task 收尾
- 新增 _post_webhook_with_retry: exponential backoff 1s/2s/4s,
  最多 4 次嘗試,5xx/408/429 重試,其他 4xx 立即放棄
- TEAMS_NOTIFY_WEBHOOK_URL env var:未設定退回 stub 行為 + log warning
- GRACE_PERIOD_SECONDS 改為 env override (預設 3)
- result_summary 截至 1500 字 (Adaptive Card 容納範圍),Logic App 端
  不做截斷,只負責顯示
- 通知失敗只 log,不寫回 Job Store (Q4 決定 — 通知失敗罕見且任務
  狀態以 Job Store 為準,使用者可在原 thread 主動問 check_pending_tasks)
- 沒動的:Adaptive Timeout 外殼邏輯、cancel/check_pending_tasks、
  _run_coding_agent_inner、_apply_metadata_to_state 等

VERSION: 1.7
2026.05.18 George : v1.7 Phase 3 — Adaptive Timeout Escalation
- startup() Step 3 擴展 — 加 JobStore 初始化 (Table Storage `jobs`,跟
  ConversationStore 共用同一組 Storage 帳號)。
- startup() 新增 replica_id 計算 (ACA CONTAINER_APP_REPLICA_NAME → hostname
  fallback),供 Job entity ReplicaId 欄位使用。
- run_workflow() 從「直接同步等」改為 adaptive timeout 外殼:
  - §5.6.4 確保 session_id 存在
  - §5.5 並行檢查 → status="rejected"
  - 起 bg task asyncio.create_task(_run_coding_agent_inner(...))
  - wait_for(event.wait(), timeout=ADAPTIVE_TIMEOUT_SECONDS=80)
  - §3.4.1 race window 修正
  - 完成 → 同步路徑回 result;timeout → detach 回 status="running"
- 新增 _run_coding_agent_inner — v1.6 run_workflow 主體搬進來,加結尾
  Job Store 終態更新 + _deliver_result_or_notify 通知分流。
- 新增 _deliver_result_or_notify — §3.5 grace period + 重檢 waiter 邏輯,
  Phase 1 驗證 4 已驗證。
- 新增 _send_teams_notification_stub — Phase 4 才接真實 notifier。
- 新增 cancel_pending_task / check_pending_tasks — §5.3 / §5.7 業務邏輯,
  mcp_server.py 的 MCP tool 對應入口。雙路徑 cancel (Q4 已敲定):
  Job Store cancel flag + executor.cancel(session_id) 同時做。
- sync_skills() re-inject 增加註解:JobStore 不掛在 _workflow,不需 re-inject。
- 沒動的:_apply_metadata_to_state / _inject_session_context /
  _build_turn_summary / approve_pending_skill / reject_pending_skill /
  list_skills / clear_session / is_ready。
- ⚠️ 已被 v1.10 取代:list_skills 於 2026.07.05 為 Dynamic Skills (Mode B)
  全面改寫 (改 async + 身份分流);run_workflow 另於 2026.06.12 加掛
  companion loop (W4/W5)。

VERSION: 1.6
2026.05.12 George : v1.6 Phase 0 — 抽象介面層依賴注入
- startup() 在 create_workflow() 後額外建立三個介面實作:
  - LocalSubprocessExecutor (從原本的 execute_code() 邏輯抽出)
  - BlobOutputFileStore (從原本的 upload_results() 邏輯抽出)
  - InMemoryJobStateStore (Phase 0 空殼,Phase 3 才填邏輯)
- 注入方式採「create_workflow 返回後 attribute 賦值」,不改 create_workflow 簽名
- BlobOutputFileStore 沿用既有 AZURE_STORAGE_ACCOUNT_* 環境變數,
  缺設定時 file_store=None,維持「沒帳號就 uploads=[] 不 crash」既有行為
- 為什麼介面建立放在 startup() 而非 create_workflow():
  1. 三個介面跟 LLM agent 創建是不同職責,分開更清晰
  2. core_handler 已經負責 conv_store 等基礎設施初始化,介面歸這層更一致
  3. 未來階段 2 切換到 HostedAgentExecutor 時,改 startup() 一處即可,
     不用動 code_agent_hosted.py 內部
- run_workflow() 本體零變更 (依賴注入在 startup 階段做完,run_workflow 只是
  跑 _workflow.run() — _workflow 內部使用注入後的 executor/file_store)
- ⚠️ 僅 v1.6 當下成立:run_workflow 自 v1.7 起改為 job 生命週期外殼
  (並行檢查 / Job Store create / adaptive timeout / detach),v1.8 加 JWT
  claims、v1.10 加 companion loop 與邊界投影。startup() 也已擴充
  JobStore / replica_id / SkillsProviderFactory / static skills provider。

VERSION: 1.5
2026.04.28 George: v1.5 修正跨輪 session_id / work_dir 漂移 bug
- _apply_metadata_to_state: 新增還原 session_id / work_dir / output_files
  / execution_count,並 os.makedirs 確保 work_dir 存在
- run_workflow: effective_session_id 計算延後到 _apply_metadata_to_state
  之後,確保 RowKey 與 entity 內 session_id 永遠一致
- 修正前症狀:同一 conversation 跨輪 create_new_state() 生新 uuid,導致
  state.session_id ≠ caller 傳的 session_id,work_dir 散落在多個
  /app/session_xxx/ 目錄,turn_history 累積不同路徑
- 修正後:state.session_id ≡ Table RowKey ≡ Helper Agent 的 session_id

2026.04.23 George: v1.4 Recent full outputs (N=1 sliding window)
- 新增 RECENT_FULL_OUTPUTS_WINDOW = 1(保留上 N 輪的完整輸出)
- run_workflow: 每輪結束時把 result["response"] 塞進 state.recent_full_outputs
- _apply_metadata_to_state: 從 metadata 還原 recent_full_outputs 回 state
- _inject_session_context: 新增「上一輪的完整輸出」區塊
- 解決:turn summary 只取前 5 行/300 字,導致 Data Agent 回的 markdown table
  下一輪無法回看原始資料的問題
- Helper Agent instructions 無需修改:資料透過現有的 SessionContext
  assistant message 機制注入,與 turn_history / final_code 同管道

2026.03.30 George: v1.3 HITL Skill Review
- 新增 approve_pending_skill(): 審核通過 → 從 pending 讀回 knowledge → merge 寫入 → sync
- 新增 reject_pending_skill(): 審核拒絕 → 刪除 pending
- 新增 imports: blob_read_pending_metadata, blob_delete_pending, blob_upload_skill

2026.03.13 George: v1.1 累積式 turn history
- 將 _inject_final_code_context 擴充為 _inject_session_context
  注入 turn_history 摘要 + original_user_request + final_code
- 新增 _build_turn_summary(): workflow 結束時生成本輪 turn summary
- turn_history 累積到 state.turn_history,由 conversation_store 持久化
- MAX_TURN_HISTORY = 10(保留第一輪 + 最近 N-1 輪)

2026.03.12 George: v1.0 從 main.py v4.0 抽離
- run_workflow(): 核心 workflow 執行(建 state → 載入 metadata → 注入 context → 跑 workflow → 存 metadata)
- startup(): 初始化 workflow + skills sync + conversation store
- list_skills(), sync_skills(), clear_session(): 管理指令
- is_ready(): adapter 啟動前檢查

依賴:
- code_agent_hosted (workflow engine)
- conversation_store (Table Storage 持久化)
- skills_sync (Blob Storage 同步)
"""

import os
import asyncio
import contextlib  # 2026.06.12 George : max_replicas>1 — W4/W5 companion task 收尾
import time  # 2026.06.12 George : max_replicas>1 — W2 查表 long-poll deadline
import json
import re
import socket
import uuid
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx  # 2026.05.18 George : v1.8 Phase 4 — Teams notification HTTP

from code_agent_hosted import (
    CodeAgentWorkflow,
    create_workflow,
    create_new_state,
    ConversationState,
)
from conversation_store import ConversationStore
from skills_sync import (
    sync_skills_from_blob,
    SKILLS_DIR,
    SKILL_ENTRY_FILENAME,
    list_local_skill_files,
    derive_resource_scan_args,
)

# 2026.03.30 George: v1.3 HITL — 新增 imports
from skills_sync import (
    blob_read_pending_metadata,
    blob_delete_pending,
    blob_upload_skill,
)

# 2026.03.17 George: v1.2 Identity Passthrough — OBO token exchange
from obo_helper import (
    exchange_all as obo_exchange_all,
    resolve_env_name_for_scope,
)

# 2026.05.18 George : v1.8 Phase 4 — JWT claims 解析 (oid / upn)
# 用於從 Bearer token 取得 user identity 寫進 Job Store + Teams 通知 payload。
# 純 base64 decode,不驗簽章 — 詳見 jwt_helper.py 模組註解。
from jwt_helper import (
    parse_jwt_claims,
    is_user_delegated_token,
    extract_verified_upn,
)

# 2026.09.04 George : 平台保留前綴 — 已驗證身分注入
# EAA_VERIFIED_* 一律由平台在執行期產生,呼叫端與容器環境都不得供給。
# skill 端以 os.environ["EAA_VERIFIED_USER_UPN"] 取用(禁用 .get / getenv,
# 讓身分缺席直接 KeyError 中止)— 見 README「Skill 身分使用規範」。
EAA_VERIFIED_PREFIX = "EAA_VERIFIED_"
VERIFIED_USER_UPN_ENV = "EAA_VERIFIED_USER_UPN"


def _purge_reserved_identity_env() -> None:
    """從行程環境刪除所有 EAA_VERIFIED_* 變數。

    executor 是 `os.environ.copy()` 後才 update 注入值(見 code_executor.py),
    所以容器層級設的同名變數會成為每個 subprocess 的底值。若某回合因未通過
    驗證而『不注入』,那個底值仍會留在 env 裡被 skill 讀到 —— 靜默地把一個
    未驗證的假身分餵給授權判斷,且 KeyError 那道防線永遠不會觸發。
    在此主動清掉,讓保證是結構性的,而不是靠「不要在 ACA 設這個變數」的叮嚀。
    """
    leaked = [k for k in os.environ if k.startswith(EAA_VERIFIED_PREFIX)]
    for key in leaked:
        os.environ.pop(key, None)
    if leaked:
        logger.warning(
            f"[Identity] Purged {len(leaked)} reserved env var(s) inherited from "
            f"the container environment: {leaked}. "
            f"{EAA_VERIFIED_PREFIX}* must never be configured on the platform "
            f"— they are injected per-turn from the verified token only."
        )

# 2026.05.12 George : v1.6 Phase 0 — 抽象介面層
# 三個介面分別對應 design doc §17 的三個 Protocol。
# 本階段只用階段 1 實作 (Local/Blob/InMemory),階段 2 才會出現 Hosted* 版本。
from code_executor import CodeExecutor, LocalSubprocessExecutor
from output_file_store import OutputFileStore, BlobOutputFileStore
from job_state_store import JobStateStore, InMemoryJobStateStore

# 2026.05.18 George : v1.7 Phase 3 — Adaptive Timeout Escalation
# JobStore 是持久層 (Table Storage),跟 in-process 的 JobStateStore 不同。
# 在 core_handler 這層直接持有 (不注入 workflow),供 run_workflow /
# cancel_pending_task / check_pending_tasks 使用。
from job_store import JobStore, JobStatus

# 2026.05.22 George : v1.9 — Static SkillsProvider (binary flag mode)
# DYNAMIC_SKILLS_ENABLED=false 時用,把 SKILLS_DIR 內所有本地 skill 全部
# 載入給 coding_agent。Dynamic 模式 (=true) 走 SkillsProviderFactory 不用這個。
# 對齊 code_agent_hosted.py 的 SkillsProvider/FileAgentSkillsProvider 雙路徑 import,
# RC3 跟更舊版本都吃得到。
try:
    from agent_framework import SkillsProvider as _StaticSkillsProvider
    _STATIC_PROVIDER_AVAILABLE = True
except ImportError:
    try:
        from agent_framework import FileAgentSkillsProvider as _StaticSkillsProvider
        _STATIC_PROVIDER_AVAILABLE = True
    except ImportError:
        _STATIC_PROVIDER_AVAILABLE = False
        _StaticSkillsProvider = None

# 2026.08.15 George : Mode A 也要有 resource_name 前綴容錯 —— 兩模式行為必須一致。
# 拿不到就退回原本的 provider(只少了容錯,不至於讓 Mode A 起不來)。
try:
    from skills_provider_factory import PrefixTolerantSkillsProvider as _TolerantSkillsProvider
    _TOLERANT_PROVIDER_IMPORT_ERROR = None
except Exception as _e:  # noqa: BLE001
    _TolerantSkillsProvider = None
    _TOLERANT_PROVIDER_IMPORT_ERROR = _e

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv())
except ImportError:
    pass

logger = logging.getLogger(__name__)

if _TOLERANT_PROVIDER_IMPORT_ERROR is not None:
    logger.warning(
        "[Skills] PrefixTolerantSkillsProvider unavailable: %s",
        _TOLERANT_PROVIDER_IMPORT_ERROR,
    )


# ============================================================================
# GLOBALS(在 startup 時初始化)
# ============================================================================

_workflow: Optional[CodeAgentWorkflow] = None
_conv_store: Optional[ConversationStore] = None
_credential = None
_project_client = None

# 2026.05.12 George : v1.6 Phase 0 — 抽象介面層 globals
# 在 startup() 中初始化後注入到 _workflow。對外暴露,供 mcp_server.py 在啟動
# 完成後可印 DI 狀態 log,以及 Phase 3 cancel_pending_task 等 MCP tool 直接
# 取用 _executor / _job_state_store。
_executor: Optional[CodeExecutor] = None
_file_store: Optional[OutputFileStore] = None
_job_state_store: Optional[JobStateStore] = None

# 2026.05.22 George : v1.8 Phase — Per-turn dynamic SkillsProvider
# Factory 是 singleton(內部持有 BlobServiceClient,跨 request 共用)。
# 在 startup() 中初始化後注入到 _workflow.skills_factory。
# 對外暴露,供 mcp_server.py 印 DI 狀態 log 用。
_skills_factory = None

# 2026.05.18 George : v1.7 Phase 3 — Adaptive Timeout Escalation
# Job Store 是 Table Storage 持久層,跟 in-process 的 _job_state_store 不同。
# 對外暴露,供 mcp_server.py 的 cancel_pending_task / check_pending_tasks
# MCP tool 直接取用。
_job_store: Optional[JobStore] = None

# 2026.05.18 George : v1.7 Phase 3
# replica_id 在 startup 計算一次,寫入 Job entity 的 ReplicaId 欄位用。
# ACA 環境下 CONTAINER_APP_REPLICA_NAME 是平台注入的,fallback 用 hostname。
# 本階段 max_replicas=1,replica_id 主要供 audit / 未來跨 replica 路徑用。
_replica_id: str = ""

# 2026.06.12 George : max_replicas>1 — W5/W6 本 replica in-flight 登記簿
# job_id → session_id。bg task 的 _workflow.run 期間在簿 (與 companion loop /
# subprocess 同生命週期)。供 W6 SIGTERM handler 列舉本 replica 仍在跑的 job、
# 標 interrupted 並砍 subprocess (下一個 commit 接 SIGTERM 時消費此簿)。
# 純 in-process,不跨 replica;跨 replica 的死亡偵測走 W5 orphan (heartbeat)。
_inflight_jobs: Dict[str, str] = {}

# 2026.05.18 George : v1.7 Phase 3
# Adaptive Timeout 同步等待秒數。短任務 80 秒內完成走同步路徑,
# 超過則 detach 走 Teams 通知。可調區間 60~90 秒,主文件 §3.1。
#ADAPTIVE_TIMEOUT_SECONDS = 80
ADAPTIVE_TIMEOUT_SECONDS = int(os.environ.get("ADAPTIVE_TIMEOUT_SECONDS", "80"))

# 2026.05.18 George : v1.7 Phase 3
# §3.5 Grace period 秒數,bg task 完成後等使用者重新註冊 waiter 的時間。
# 主文件 §3.5.2 暫定 3 秒,實測調整 (2~5 秒區間)。
# 2026.05.18 George : v1.8 Phase 4 — 改為 env override
GRACE_PERIOD_SECONDS = int(os.environ.get("GRACE_PERIOD_SECONDS", "3"))

# 2026.06.12 George : max_replicas>1 — W2 / W7
# check_pending_tasks 查表 long-poll 的輪詢間隔 (秒)。取代原 in-process
# asyncio.Event 等待,讓「job 跑在 instance A、check 落在 instance B」也能
# 正確等到結果 (Table Storage 為唯一 SoR,任何 instance 都查得到)。
# 取捨:結果新鮮度延遲上限 = 此間隔 (秒級,實務無感);每輪一次 Table point
# query,成本噪音等級。
# ⚠️ 約束:CHECK_POLL_INTERVAL < GRACE_PERIOD_SECONDS,否則 Teams 去重可能失效
#   —— bg task 完成後的 grace 窗內,checker 至少要輪詢一次才能標 picked_up,
#   讓 _deliver_result_or_notify 回讀到並跳過 Teams (見該函式註解)。
#   預設 2 < 3 ✓。
CHECK_POLL_INTERVAL = int(os.environ.get("CHECK_POLL_INTERVAL", "2"))

# 2026.06.12 George : max_replicas>1 — W4/W5/W7 owner 端伴隨迴圈調參
# 一個迴圈同時做 cancel flag 輪詢 (W4) 與 heartbeat 寫入 (W5):
#   - 迴圈 tick = CANCEL_POLL_INTERVAL
#   - heartbeat 每 HEARTBEAT_INTERVAL 寫一次 (按 monotonic 計時,非每 tick)
# 約束:
#   - HEARTBEAT_INTERVAL ≪ ORPHAN_TIMEOUT_SEC (預設 ~4.5 倍),否則暫時性
#     漏寫會被誤判孤兒。
#   - 取消延遲上限 ≈ CANCEL_POLL_INTERVAL + subprocess 終止 grace
#     (見 code_executor.cancel 的 SIGTERM→30s→SIGKILL)。
CANCEL_POLL_INTERVAL = int(os.environ.get("CANCEL_POLL_INTERVAL", "5"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "20"))
ORPHAN_TIMEOUT_SEC = int(os.environ.get("ORPHAN_TIMEOUT_SEC", "90"))

# 2026.05.18 George : v1.8 Phase 4 — Teams 通知 webhook
# Logic App HTTP trigger URL,bg task 完成且無 waiter 時 POST 到此 URL。
# 未設定時 fallback 為「只 log 不發 HTTP」,並於 startup 記 warning。
TEAMS_NOTIFY_WEBHOOK_URL = os.environ.get("TEAMS_NOTIFY_WEBHOOK_URL", "")

# 2026.05.18 George : v1.8 Phase 4 — 通知重試常數
# 通知是 fire-and-forget,失敗最多重試 3 次 (共 4 次嘗試),間隔 1/2/4 秒
# exponential backoff。HTTP 5xx 重試,4xx 立即放棄 (payload 結構問題重試
# 也救不回來)。
TEAMS_NOTIFY_MAX_ATTEMPTS = 4  # 1 次首發 + 3 次重試
TEAMS_NOTIFY_TIMEOUT_SECONDS = 10.0
TEAMS_NOTIFY_RESULT_SUMMARY_MAX_CHARS = 1500  # 截至 Adaptive Card 容納範圍


# ============================================================================
# 2026.05.22 George : v1.9 — Static SkillsProvider builder
# ============================================================================
# 用於 DYNAMIC_SKILLS_ENABLED=false 模式 (主路徑) — 把 SKILLS_DIR 內所有本地
# skill 載入給 coding_agent.context_providers,讓所有 skill 全開、無 RBAC。
#
# sync_skills() 也會用同一個 helper 在 Blob → 本地 sync 後重建 provider
# 並 re-inject (因為 SKILLS_DIR 內容變了)。
#
# 回傳 None 的情境 (coding_agent 退回無 skill 模式):
# - SkillsProvider class 載不到 (agent-framework 版本問題)
# - SKILLS_DIR 不存在 (sync_skills_from_blob 沒跑或失敗)
# - 建構 provider 時拋例外
# ============================================================================
def _build_static_skills_provider():
    if not _STATIC_PROVIDER_AVAILABLE:
        logger.warning(
            "[Skills] Static provider unavailable: SkillsProvider class not importable"
        )
        return None

    if not os.path.isdir(SKILLS_DIR):
        logger.warning(
            "[Skills] Static provider unavailable: SKILLS_DIR=%s not found",
            SKILLS_DIR,
        )
        return None

    try:
        #2026.06.29: 對齊 1.8.0 寫法
        # 2026.08.15 George : MAF 1.8.0 預設只認 7 種副檔名、只掃 references/
        # 與 assets/ 且不遞迴 —— .css/.html 與巢狀資產會落地却註冊不到,
        # read_skill_resource 就回 "Resource not found"。改由實際檔案反推兩個
        # 掃描參數讓白名單失效。【Mode B 的 skills_provider_factory 必須保持
        # 同步】,否則兩模式的 skill 可見行為會不一致。
        resource_exts, resource_dirs = derive_resource_scan_args(SKILLS_DIR)
        provider_cls = _TolerantSkillsProvider or _StaticSkillsProvider
        provider = provider_cls.from_paths(
            SKILLS_DIR,
            resource_extensions=resource_exts,
            resource_directories=resource_dirs,
        )
        provider.resource_directories = resource_dirs
        # 列出載入的 skill 名稱,方便 debug
        skill_names = sorted(
            d for d in os.listdir(SKILLS_DIR)
            if os.path.isdir(os.path.join(SKILLS_DIR, d))
        )
        logger.info(
            "[Skills] Static provider built from %s (%d skills: %s) "
            "resource_extensions=%r resource_directories=%r",
            SKILLS_DIR, len(skill_names), ", ".join(skill_names) or "(none)",
            resource_exts, resource_dirs,
        )
        return provider
    except Exception as e:
        logger.error(
            "[Skills] Failed to build static provider: %s", e, exc_info=True,
        )
        return None


def is_ready() -> bool:
    """檢查 core 是否已初始化。"""
    return _workflow is not None


# ============================================================================
# STARTUP
# ============================================================================

async def startup():
    """
    初始化 workflow + sync skills + conversation store。
    由各 adapter 的 startup 呼叫。

    2026.05.12 George : v1.6 Phase 0 — 在 step 4 額外建立並注入
    三個介面實作 (CodeExecutor / OutputFileStore / JobStateStore)。

    2026.05.18 George : v1.7 Phase 3 — Step 3 擴展 JobStore (持久層)
    + 計算 replica_id;新增 step 5 為 cancel checkpoint 等 Phase 3 業務邏輯
    準備好所有依賴。
    """
    global _workflow, _conv_store, _credential, _project_client
    # 2026.05.12 George : v1.6 Phase 0
    global _executor, _file_store, _job_state_store
    # 2026.05.18 George : v1.7 Phase 3
    global _job_store, _replica_id
    # 2026.05.22 George : v1.8 — SkillsProviderFactory
    global _skills_factory

    logger.info("=" * 60)
    logger.info("Core Handler - Starting...")
    logger.info("=" * 60)

    # 2026.09.04 George : 必須早於任何 skill 執行 — 見函式 docstring
    _purge_reserved_identity_env()

    # 2026.05.18 George : v1.7 Phase 3
    # 2026.06.12 George : max_replicas>1 — replica_id 雙平台 fallback
    # 計算 replica_id (寫進 Job entity 的 ReplicaId 欄位,供 audit / heartbeat
    # 歸因)。平台注入的環境變數因平台而異:
    #   - ACA:         CONTAINER_APP_REPLICA_NAME
    #   - App Service: WEBSITE_INSTANCE_ID (64-char instance hash)
    #   - 本地測試:    hostname
    # ⚠️ 正確性不依賴此值:W5 orphan 判定靠 heartbeat 時間戳,不靠 replica_id
    #    比對。此值僅供 audit / log 歸因,所以兩平台各認一個即可。
    _replica_id = (
        os.environ.get("CONTAINER_APP_REPLICA_NAME")
        or os.environ.get("WEBSITE_INSTANCE_ID")
        or socket.gethostname()
        or "unknown"
    )
    logger.info(f"[startup] replica_id={_replica_id}")

    # 1. 從 Blob Storage 同步 skills 到本地
    try:
        count = await sync_skills_from_blob()
        logger.info(f"Skills sync: {count} skills loaded from Blob Storage")
    except Exception as e:
        logger.warning(f"Skills sync failed (will use local skills only): {e}")

    # 2. 初始化 workflow
    from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
    _credential = AsyncDefaultAzureCredential()
    _workflow, _project_client = await create_workflow(_credential)
    logger.info("Workflow initialized")

    # 3. 初始化 Conversation Store + Job Store (兩者共用同一組 Storage 帳號)
    account_name = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
    if account_name:
        _conv_store = ConversationStore(account_name)
        await _conv_store.ensure_table()
        logger.info("Conversation Store initialized (Table Storage)")

        _job_store = JobStore(account_name)
        await _job_store.ensure_table()
        logger.info("Job Store initialized (Table Storage)")
    else:
        logger.warning(
            "AZURE_STORAGE_ACCOUNT_NAME not set, "
            "workflow metadata will not persist across requests"
        )


        # _job_store 留 None。run_workflow 進入時會檢查並回報 config 錯誤,
        # 而不是 silently 跳過 — adaptive timeout 機制依賴 persistent job
        # state,沒有它整套都不能用。

    # 4. (Phase 0) 建立抽象介面實作並注入 workflow
    # 2026.05.12 George : v1.6 Phase 0
    # 注入方式:create_workflow() 返回後直接 setattr,不改 workflow 簽名。
    # 這讓 code_agent_hosted.py 的 CodeAgentWorkflow 變更只是「多兩個 optional
    # attribute」,不影響既有 unit test 與 main.py adapter 的呼叫方式。

    # 4a. CodeExecutor: 階段 1 用 LocalSubprocessExecutor
    # 階段 2 POC 通過後可改成 HostedAgentExecutor,這裡是唯一改動點
    _executor = LocalSubprocessExecutor()
    _workflow.executor = _executor
    logger.info(f"[startup] CodeExecutor injected: {type(_executor).__name__}")

    # 4b. OutputFileStore: 沿用既有 Storage 帳號,缺設定時 file_store=None
    # 維持「沒帳號就 uploads=[] 不 crash」的既有行為 (upload_results() 原本
    # 在缺帳號時就 return []),Phase 0 不改變此行為
    if account_name:
        _file_store = BlobOutputFileStore(
            account_name=account_name,
            container_name="code-outputs",
        )
        _workflow.file_store = _file_store
        logger.info(
            f"[startup] OutputFileStore injected: {type(_file_store).__name__}, "
            f"container=code-outputs"
        )
    else:
        _file_store = None
        _workflow.file_store = None
        logger.warning(
            "[startup] OutputFileStore not configured (AZURE_STORAGE_ACCOUNT_NAME missing); "
            "file uploads will be skipped"
        )        

    # 4c. JobStateStore: in-process waiter 機制
    # Phase 0 是空殼,Phase 3 已填完整邏輯 (見 job_state_store.py)。
    _job_state_store = InMemoryJobStateStore()
    _workflow.job_state_store = _job_state_store
    logger.info(
        f"[startup] JobStateStore injected: {type(_job_state_store).__name__}"
    )

    # ============================================================
    # 4d. SkillsProvider wiring
    # 2026.05.22 George : v1.8 — dynamic per-turn factory (初版)
    # 2026.05.22 George : v1.9 — Binary flag, no fallback
    # ============================================================
    # 單一 env var 控制:
    #   DYNAMIC_SKILLS_ENABLED (預設 false)
    #
    # Flag=false (預設,主路徑):
    #   建 static FileAgentSkillsProvider(SKILLS_DIR) 並注入
    #   coding_agent.context_providers。所有本地 skill 全開,無 RBAC。
    #   workflow.run() 內 if self.skills_factory is not None 為 False,
    #   不會 swap context_providers,coding_agent 永久使用這個 static provider。
    #
    # Flag=true:
    #   建 SkillsProviderFactory 並注入 workflow.skills_factory。
    #   workflow.run() per-turn 用 factory 拉 RLS-filtered skill 並 swap。
    #
    # 故意不做 SQL/Blob 故障自動 fallback (設計取捨):
    #   - 程式只負責「我有沒有 RBAC」的二元決策
    #   - 「dependency 故障了怎麼辦」是 ops 的事:翻 flag → 重啟新 revision
    #   - 避免「自動繞過 RBAC」的 security 灰色地帶
    #   故障時 factory 內部 yield 空 provider,turn 仍能跑完但 LLM 沒工具,
    #   使用者反映 → ops 把 DYNAMIC_SKILLS_ENABLED 改 false 重啟即可。
    # ============================================================
    _dynamic_enabled = os.environ.get(
        "DYNAMIC_SKILLS_ENABLED", "false"
    ).lower() == "true"

    if not _dynamic_enabled:
        # ── Static-only 模式 (主路徑) ──
        _skills_factory = None
        _workflow.skills_factory = None

        _static_provider = _build_static_skills_provider()
        if _static_provider is not None:
            try:
                existing = list(
                    _workflow.coding_agent.context_providers or []
                )
                # 過濾掉舊的 SkillsProvider (理論上不會有,保險)
                non_skill = [
                    p for p in existing
                    if not isinstance(p, _StaticSkillsProvider)
                ]
                _workflow.coding_agent.context_providers = (
                    non_skill + [_static_provider]
                )
                logger.info(
                    "[Phase v1.9] DYNAMIC_SKILLS_ENABLED=false — "
                    "static SkillsProvider injected (all local skills available, no RBAC). "
                    "Total context_providers: %d",
                    len(_workflow.coding_agent.context_providers),
                )
            except Exception as e:
                logger.error(
                    "[Phase v1.9] Failed to inject static provider: %s "
                    "(coding_agent runs without skills)",
                    e, exc_info=True,
                )
        else:
            logger.warning(
                "[Phase v1.9] static provider unavailable (see warnings above) — "
                "coding_agent runs without skills"
            )
    else:
        # ── Dynamic 模式 ──
        try:
            from skills_provider_factory import SkillsProviderFactory
            from azure.storage.blob.aio import BlobServiceClient
            from azure.identity.aio import DefaultAzureCredential as _BlobCred

            # 改用 AZURE_STORAGE_ACCOUNT_NAME 組合出 URL
            account_name = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME", "")
            if not account_name:
                raise ValueError(
                    "DYNAMIC_SKILLS_ENABLED=true but AZURE_STORAGE_ACCOUNT_NAME not set"
                )
            
            _blob_account = f"https://{account_name}.blob.core.windows.net"

            _blob_client = BlobServiceClient(
                account_url=_blob_account,
                credential=_BlobCred(),
            )


            _skills_factory = SkillsProviderFactory(
                blob_client=_blob_client,
                blob_container=os.environ.get("SKILLS_BLOB_CONTAINER", "skills"),
            )
            _workflow.skills_factory = _skills_factory
            logger.info(
                "[Phase v1.9] DYNAMIC_SKILLS_ENABLED=true, "
                "SkillsProviderFactory injected: blob=%s, container=%s, "
                "sql_server=%s, sql_database=%s",
                _blob_account,
                os.environ.get("SKILLS_BLOB_CONTAINER", "skills"),
                os.environ.get("AZURE_SQL_SERVER", "(unset)"),
                os.environ.get("AZURE_SQL_DATABASE", "(unset)"),
            )
        except Exception as e:
            _skills_factory = None
            _workflow.skills_factory = None
            logger.error(
                "[Phase v1.9] Failed to init SkillsProviderFactory: %s "
                "(workflow.run() will skip SkillsProvider, "
                "coding_agent runs without skills — "
                "consider rolling back DYNAMIC_SKILLS_ENABLED to false)",
                e, exc_info=True,
            )

    # 2026.05.18 George : v1.8 Phase 4 — Teams 通知 webhook 狀態
    # 不設定不阻斷啟動 (退回「只 log 不發 HTTP」),但記 warning 提醒
    if TEAMS_NOTIFY_WEBHOOK_URL:
        # URL 可能含 sig= 等敏感參數,只 log host + path 前綴
        url_preview = TEAMS_NOTIFY_WEBHOOK_URL.split("?")[0][:80]
        logger.info(
            f"[startup] Teams notification webhook configured: "
            f"{url_preview}..."
        )
    else:
        logger.warning(
            "[startup] TEAMS_NOTIFY_WEBHOOK_URL not set — Teams notifications "
            "will fall back to log-only. Set this env var to enable "
            "real notifications via Logic App."
        )
    logger.info(
        f"[startup] GRACE_PERIOD_SECONDS={GRACE_PERIOD_SECONDS}, "
        f"ADAPTIVE_TIMEOUT_SECONDS={ADAPTIVE_TIMEOUT_SECONDS}"
    )

    logger.info("Core Handler ready")


# ============================================================================
# CORE: Workflow execution
# ============================================================================

# 2026.03.13 George: 累積式 turn history 上限
MAX_TURN_HISTORY = 10

# 2026.04.23 George: v1.4 Recent full outputs sliding window 大小
# N=1 代表只保留「上一輪」的完整輸出,給下一輪回看用
# 未來若需擴大(例如 N=3),只需改這個常數,conversation_store schema 不用動
RECENT_FULL_OUTPUTS_WINDOW = 1


# 2026.06.01 George : v1.10 MCP 邊界回傳投影 (Boundary Response Projection)
# ───────────────────────────────────────────────────────────────────────────
# 問題 (room-finder bug):_workflow.run() 回傳的 result 是「執行包絡」,
# 除了 response/success/uploads 這些 Helper Agent 真正需要的欄位,還夾帶
# coding_response_raw (14KB 完整腳本)、script_path、raw_stdout、agent_message、
# succeeded_count 等診斷欄位。這些東西同步回到 Helper Agent 的 context 後:
#   1. 14KB 腳本的 salience 把 1KB 的真正結果蓋過 → Helper anchor 到腳本,
#      回「只產出可重跑腳本」而不是列結果。
#   2. succeeded_count=0 與 status=success 矛盾 → 誤導 Helper 判定沒成功。
#   3. skills_referenced 夾雜不相關 skill → 噪音。
# 這不是 prompt 能穩定壓制的事 (prompt 在跟 payload shape 對抗,payload 贏)。
#
# 修法:在同步回傳離開 run_workflow 之前,做一層 whitelist 投影,只留下
# 主文件 §contract 宣告的 success-shape 欄位 + 少量 metadata。
# 腳本 / raw_stdout / agent_message 一律剝掉 — Helper 的工作是 parse response,
# 不是讀 code。
#
# ⚠️ 只作用於「同步回給 MCP 邊界」的 result。bg-task 路徑 (_run_coding_agent_inner
# 收尾) 寫進 Job Store 的是未投影的完整 result,保留完整診斷包絡供 audit。
RESPONSE_BOUNDARY_WHITELIST = frozenset({
    "success",
    "response",
    "uploads",
    "needs_input",
    "session_id",
    "skills_referenced",
    # 2026.08.31 George : route_only — model 實際 read_skill_resource 的清單。
    # 只有 route_only 的短路 payload 會建立這個 key;execute 路徑的 result 從不含它。
    # 本 whitelist 是「保留清單」不是「補齊清單」,多列一個從不存在的 key 對既有
    # 流量是 no-op。
    "loaded_resources",
    # detach / rejected / cancelled 等非 success shape 的控制欄位也保留,
    # 讓 mcp_server._build_response_payload 仍能正確分流。
    "status",
    "job_id",
    "task_description",
    "message",
    "reason",
    "existing_job",
})


def _project_boundary_result(result: dict) -> dict:
    """
    把 workflow result 投影成 MCP 邊界 (Helper Agent) 該看到的最小 shape。

    分兩步:
      (1) Normalize — 確保答案主體落在 `response`、成功旗標落在 `success`。
          這修的是 room-finder bug 的真正根因:執行包絡常把答案放在
          `agent_message` / `raw_stdout`,而契約 (主文件 ~§647) 規定答案
          要在 `response`。若直接 whitelist 會把唯一一份答案一起剝掉。
      (2) Strip — 只保留 RESPONSE_BOUNDARY_WHITELIST 內的 key,剝掉
          coding_response_raw / script_path / raw_stdout / agent_message /
          succeeded_count 等診斷欄位。

    防呆:
    - 非 dict (理論上不會發生) 直接原樣回傳,不爆。
    - 已正確帶 `response` 的 result (走標準契約的路徑) 不被覆蓋。
    """
    if not isinstance(result, dict):
        return result

    # ── (1) Normalize ──────────────────────────────────────────────
    # answer body 優先序:既有 response > agent_message > raw_stdout
    response_body = result.get("response")
    if not (isinstance(response_body, str) and response_body.strip()):
        fallback_body = (
            result.get("agent_message")
            or result.get("raw_stdout")
            or ""
        )
        if isinstance(fallback_body, str):
            body = fallback_body.strip()
            # 剝掉 Coding Agent 的成功標記前綴 (主文件 Proxy Pattern 特例)
            for marker in ("✅ EXECUTION SUCCESSFUL:", "EXECUTION SUCCESSFUL:"):
                if body.startswith(marker):
                    body = body[len(marker):].lstrip()
                    break
            response_body = body
        else:
            response_body = ""
        result = {**result, "response": response_body}
        logger.info(
            "[boundary-projection] normalized answer body into 'response' "
            "from agent_message/raw_stdout (response was empty)"
        )

    # success 旗標:既有 success 優先,否則從 status 推導
    if "success" not in result:
        status_val = result.get("status")
        if status_val is not None:
            result = {**result, "success": (status_val == "success")}

    # ── (2) Strip ──────────────────────────────────────────────────
    projected = {
        k: v for k, v in result.items()
        if k in RESPONSE_BOUNDARY_WHITELIST
    }

    # 記一筆被剝掉的 key,方便日後追問題 (只 log key 名,不 log 內容)
    dropped = [k for k in result.keys() if k not in RESPONSE_BOUNDARY_WHITELIST]
    if dropped:
        logger.info(
            f"[boundary-projection] dropped {len(dropped)} diagnostic "
            f"field(s) from sync result: {dropped}"
        )

    return projected


# 2026.08.31 George : route_only — mode 回顯
# ─────────────────────────────────────────────────────────────────────────
# 為什麼在 _project_boundary_result 「之後」注入,而不是把 "mode" 加進 whitelist:
#   (1) whitelist 只作用在正常完成的 result;rejected / running 這些非完成 shape
#       根本不經過投影。呼叫端要靠回顯判斷「新版是否生效」,漏掉這些 shape 會讓
#       他們把 rejected 誤判成「舊版靜默忽略了 mode 參數」。
#   (2) 不動 whitelist = 不新增任何欄位外洩面。
# execute(預設)時原樣回傳「同一個物件」—— 不 copy、不加 key,既有 caller 收到的
# JSON 與改動前 byte-identical。
def _echo_mode(payload: dict, mode: str) -> dict:
    if mode == "execute" or not isinstance(payload, dict):
        return payload
    return {**payload, "mode": mode}


async def run_workflow(
    user_input: str,
    session_id: str = None,
    credentials: dict = None,
    # 2026.08.31 George : route_only — "execute"(預設,現行行為) | "route_only"
    # route_only 只做 skill 路由判定就收工,不執行任何生成的程式碼。
    # 用途見 code_agent_hosted._run_turn_loop 的短路點註解。
    # 預設值確保所有既有 caller(main.py 的兩處、MCP tool)行為完全不變。
    mode: str = "execute",
    # 2026.09.04 George : scenario 收斂 — 宿主選定的情境層 skill 名稱。
    # 空字串 = 不收斂(現行行為)。驗證在 SkillsProviderFactory 那一層做且全程
    # fail-open —— 這裡不擋。
    scenario: str = "",
) -> dict:
    """
    執行 workflow,回傳 result dict。

    Protocol-agnostic — adapter 負責從各自的 request format 提取參數。

    2026.05.18 George : v1.7 Phase 3 — Adaptive Timeout Escalation
    本函式從「直接同步等」改為 adaptive timeout 外殼:
    - 短任務 (< 80 秒):同步路徑,行為等同 Phase 0 (v1.6) 完全相容
    - 長任務 (≥ 80 秒):detach 進入 bg task,回傳 status="running" + job_id,
      bg task 完成後若無 waiter 則推 Teams 通知 (Phase 4 才接真實 notifier)
    - 並行任務 (同 session 已有 running job):回傳 status="rejected"

    對應主文件 v3 §3 Adaptive Timeout Escalation、§5.5 並行控制、
    §3.4.1 race window 修正、§13.2 完整骨架。

    Args:
        user_input: 用戶的請求文字
        session_id: 可選的 session ID(用於跨 invocation 延續 metadata)
        credentials: 可選的 credentials dict(注入為環境變數)
        scenario: 可選。宿主選定的情境層 skill 名稱,決定本輪載哪些能力層
                  skill。不隨 session 持久化,宿主每輪都要重帶。

    Returns:
        以下三種 shape 之一:

        正常完成 (短任務 / race window 修正):
            {success, response, uploads, needs_input, session_id,
             skills_referenced, turn_history (optional)}

        長任務 detach:
            {status: "running", job_id, session_id, task_description, message}

        並行被拒:
            {status: "rejected", reason, session_id, existing_job, message}

        mcp_server.py 的 _build_response_payload 會根據 shape 分流。
    """
    # ─────────────────────────────────────────────────────────────
    # Pre-check: Job Store 必須已初始化才能跑 adaptive timeout。
    # 若 _job_store 是 None (Storage 帳號沒設定),整個 adaptive timeout
    # 機制都無法運作,只能 fallback 到「直接跑」的舊行為。本階段假設
    # production 一定有 _job_store,本 fallback 路徑只供開發 / 測試用。
    # ─────────────────────────────────────────────────────────────
    if _job_store is None or _job_state_store is None:
        logger.warning(
            "[run_workflow] _job_store or _job_state_store not initialized, "
            "falling back to direct synchronous execution (adaptive timeout "
            "disabled). This should only happen in dev/test without Storage."
        )
        fallback_result = await _run_coding_agent_inner(
            user_input=user_input,
            session_id_hint=session_id,
            credentials=credentials,
            job_id=None,  # 沒 job 追蹤
            mode=mode,
            scenario=scenario,
        )
        # v1.10: dev/test fallback 也是同步回邊界 → 投影
        return _echo_mode(_project_boundary_result(fallback_result), mode)

    # ─────────────────────────────────────────────────────────────
    # Step 1: 確保 session_id 存在 (主文件 §5.6.4)
    # 既有行為 (Phase 0):session_id 為 None 時,_run_coding_agent_inner
    # 內部會用 state.session_id (create_new_state 新生 uuid)。但 §5.5 並行
    # 檢查需要在 inner 跑之前就知道 session_id,所以這裡先確定。
    #
    # 兩種情境:
    #   (a) caller 傳了 session_id → 直接拿來用 (effective_session_id)
    #   (b) caller 沒傳 → 先生個新 uuid 作為 effective_session_id
    # 後者本來在 inner 內 (line _apply_metadata_to_state 後) 才決定,Phase 3
    # 提前到這裡。inner 內邏輯依然會還原 metadata 內的 session_id,只是 RowKey
    # 已經提前確定下來。
    # ─────────────────────────────────────────────────────────────
    if session_id and session_id.strip():
        effective_session_id = session_id.strip()
    else:
        # 沒傳 session_id → 開新 session (上游 v1.5 修正已避免漂移)
        effective_session_id = str(uuid.uuid4())
        logger.info(
            f"[run_workflow] New session created: {effective_session_id}"
        )

    # ─────────────────────────────────────────────────────────────
    # Step 2: §5.5 並行檢查 — 同 session 已有 running job 則拒絕
    # ─────────────────────────────────────────────────────────────
    existing = await _job_store.find_running_by_session(effective_session_id)
    if existing is not None:
        logger.info(
            f"[run_workflow] Concurrent job exists in session="
            f"{effective_session_id}, rejecting new submission. "
            f"existing_job_id={existing.job_id}, "
            f"started_at={existing.created_at}"
        )
        return _echo_mode({
            "status": "rejected",
            "reason": "concurrent_job_exists",
            "session_id": effective_session_id,
            "existing_job": {
                "job_id": existing.job_id,
                "task_description": existing.task_description,
                "started_at": existing.created_at,
            },
            "message": "你還有一個任務在進行中,請先等它完成或取消",
        }, mode)

    # ─────────────────────────────────────────────────────────────
    # Step 3: 建 job (Table Storage 持久化) + 註冊 in-process waiter
    # 2026.05.18 George : v1.8 Phase 4 — 解析 JWT claims
    # 寫進 Job Store user_id / user_email,供 Teams 通知收件人使用。
    # ─────────────────────────────────────────────────────────────
    job_id = str(uuid.uuid4())

    # task_description 取 user_input 前 200 字,給 Teams 通知卡片用
    task_description = user_input[:200] if user_input else ""

    # 2026.05.18 George : v1.8 Phase 4
    # 從 credentials.__user_token 解析 JWT claims。注意這裡用 .get() 而非
    # .pop() — _run_coding_agent_inner 的 OBO exchange 還要再用 user_token,
    # 不能在這裡 pop 掉。JWT 解析是純讀取,無副作用。
    user_oid: Optional[str] = None
    user_email: Optional[str] = None
    if credentials and credentials.get("__user_token"):
        try:
            claims = parse_jwt_claims(credentials["__user_token"])
            user_oid = claims.get("oid")
            user_email = claims.get("user_email")
            if user_oid or user_email:
                logger.info(
                    f"[run_workflow] JWT claims: "
                    f"oid={user_oid!r}, user_email={user_email!r}"
                )
            else:
                # 解析成功但 claims 為空 — token 格式可能不是預期的 Entra ID
                # JWT (例如 opaque token、或 dev/test 用的假 token)
                logger.warning(
                    "[run_workflow] JWT parsed but no oid/upn claims found "
                    "(token may not be Entra ID JWT)"
                )
        except Exception as e:
            # 解析失敗不阻斷流程 — Teams 通知會 fallback 到 user_email=None,
            # Logic App 端應有 graceful degradation
            logger.warning(
                f"[run_workflow] JWT claim extraction failed (continuing "
                f"without user identity): {type(e).__name__}: {e}"
            )

    await _job_store.create(
        session_id=effective_session_id,
        job_id=job_id,
        task_description=task_description,
        user_id=user_oid,
        user_email=user_email,
        replica_id=_replica_id,
        # conversation_ref: 本階段仍傳 None。未來若要從 Bot Framework 拿
        # conversation reference,在這裡塞。Phase 4 走 Logic App + user_email
        # 路徑,不需要 conversation_ref。
    )

    event = await _job_state_store.register_waiter(job_id)

    # ─────────────────────────────────────────────────────────────
    # Step 4: 起 bg task,asyncio.create_task 會在當前 event loop 排程,
    # handler return 之後 bg task 仍存活 (Phase 1 驗證 1 已實證)。
    # ─────────────────────────────────────────────────────────────
    bg_task = asyncio.create_task(
        _run_coding_agent_inner(
            user_input=user_input,
            session_id_hint=effective_session_id,
            credentials=credentials,
            job_id=job_id,
            mode=mode,
            scenario=scenario,
        )
    )
    # 名字方便 debug
    bg_task.set_name(f"coding-agent-{job_id[:8]}")

    # ─────────────────────────────────────────────────────────────
    # Step 5: Adaptive Timeout - 等 ADAPTIVE_TIMEOUT_SECONDS 秒看是否完成
    #
    # ⚠️ 不能直接 asyncio.wait_for(bg_task, timeout=...) — 它預設 timeout 會
    # cancel bg task。改用 wait_for(event.wait(), ...) 等 JobStateStore 的
    # waiter event,timeout 時 bg task 不受影響,繼續跑。
    # 對應主文件 §13.1。
    # ─────────────────────────────────────────────────────────────
    try:
        await asyncio.wait_for(
            event.wait(),
            timeout=ADAPTIVE_TIMEOUT_SECONDS,
        )

        # 同步完成路徑
        result = await _job_state_store.cleanup_result(job_id)
        if result is None:
            # 不該發生:event.set() 後 result 一定已寫入 _results dict。
            # 若真的發生,可能是極端 race 或 bug,回 timeout 邊界 race
            # 修正的 detach 路徑當保險。
            logger.error(
                f"[run_workflow] event set but no result for job={job_id}, "
                f"unexpected state, returning as running"
            )
            return _echo_mode(
                _build_running_response(
                    job_id, effective_session_id, task_description
                ),
                mode,
            )

        logger.info(
            f"[run_workflow] Job {job_id} completed synchronously within "
            f"{ADAPTIVE_TIMEOUT_SECONDS}s"
        )
        # v1.10: 同步完成 → 投影成 Helper Agent 該看的最小 shape (剝診斷欄位)
        return _echo_mode(_project_boundary_result(result), mode)

    except asyncio.TimeoutError:
        # ─────────────────────────────────────────────────────────
        # §3.4.1 Race window 修正:event 沒被 set,但 bg task 可能剛好
        # 在 timeout 邊界完成,result 已寫入但 event.set() 與 wait_for
        # 觸發 TimeoutError 的順序不確定。先檢查 _pending_results。
        # ─────────────────────────────────────────────────────────
        result = await _job_state_store.cleanup_result(job_id)
        if result is not None:
            logger.info(
                f"[run_workflow] Race window caught: job {job_id} completed "
                f"in timeout boundary, returning result directly"
            )
            # v1.10: race window 也是同步回邊界 → 投影
            return _echo_mode(_project_boundary_result(result), mode)

        # 真的還沒完成,detach 進入 running 狀態
        logger.info(
            f"[run_workflow] Job {job_id} exceeded "
            f"{ADAPTIVE_TIMEOUT_SECONDS}s, detaching to background"
        )
        return _echo_mode(
            _build_running_response(
                job_id, effective_session_id, task_description
            ),
            mode,
        )

    finally:
        # 無論走哪條路徑,waiter dict 都要清乾淨,避免 leak。
        await _job_state_store.cleanup_waiter(job_id)


def _build_running_response(
    job_id: str,
    session_id: str,
    task_description: str,
) -> dict:
    """組「長任務 detach」回應 (主文件 §5.2)。"""
    return {
        "status": "running",
        "job_id": job_id,
        "session_id": session_id,
        "task_description": task_description,
        "message": "任務較長,完成後會主動通知你",
    }


def _register_inflight(job_id: str, session_id: str) -> None:
    """登記本 replica 正在跑的 job (W5/W6)。"""
    _inflight_jobs[job_id] = session_id


def _deregister_inflight(job_id: str) -> None:
    """移除登記 (job 的 _workflow.run 結束時)。"""
    _inflight_jobs.pop(job_id, None)


async def shutdown() -> None:
    """釋放長駐 aio 資源 (Table Storage client + credential + workflow credential)。

    2026.07.20 George : 效能 #1 第二波 — job_store / conversation_store 遷移
    azure.data.tables.aio 後,client 與 credential 為長駐單例,需在 app
    shutdown 時顯式 close,避免 aiohttp session / credential 未關閉警告與
    連線洩漏。由 adapter 的 lifespan shutdown 呼叫 (shutdown_inflight_jobs
    之後,確保 durable 中斷標記已寫完才關 client)。

    2026.07.20 George : 同批補關 workflow 用的 _credential (azure.identity.aio
    的 AsyncDefaultAzureCredential,startup 建立、供 create_workflow /
    _project_client 使用),原本從未 close。
    2026.07.20 George : #3 落地 — 一併補關 _admin_credential (Mode B 寫路徑用 MI
    拿 SQL admin token 的長駐 credential),原本從未 close。
    """
    for name, store in (("job_store", _job_store), ("conv_store", _conv_store)):
        if store is not None:
            try:
                await store.close()
                logger.info(f"[shutdown] {name} closed")
            except Exception as e:
                logger.warning(f"[shutdown] {name}.close() error: {e}")

    if _credential is not None:
        try:
            await _credential.close()
            logger.info("[shutdown] workflow credential closed")
        except Exception as e:
            logger.warning(f"[shutdown] _credential.close() error: {e}")

    if _admin_credential is not None:
        try:
            await _admin_credential.close()
            logger.info("[shutdown] admin credential closed")
        except Exception as e:
            logger.warning(f"[shutdown] _admin_credential.close() error: {e}")

    # 2026.07.25 George : #5 — 關閉 debug bundle 的 cached blob client/credential/
    # executor(sync helper,卸載到 thread 避免阻塞 shutdown loop)。
    try:
        from code_agent_hosted import close_debug_bundle_client
        await asyncio.to_thread(close_debug_bundle_client)
        logger.info("[shutdown] debug bundle client closed")
    except Exception as e:
        logger.warning(f"[shutdown] close_debug_bundle_client error: {e}")


async def shutdown_inflight_jobs(
    reason: str = "shutdown",
    cancel_timeout: float = 3.0,
) -> int:
    """W6 — SIGTERM / graceful shutdown 時把本 replica 仍在跑的 job 標
    INTERRUPTED,並 best-effort 砍 subprocess。

    2026.06.12 George : max_replicas>1 — W6。

    由 mcp_server 的 lifespan shutdown 呼叫 (uvicorn 收 SIGTERM → graceful
    shutdown → lifespan 退出時)。觸發來源:scale-in、revision 替換 / 部署、
    平台維護。

    兩段式 (front-load durable work):
      Pass 1:把所有 in-flight job 標 Table INTERRUPTED —— durable、關鍵,
              先做完,確保即使 container 隨即被硬殺,中斷狀態已落 Table。
      Pass 2:best-effort 對每個 session 發 executor.cancel,但用短 timeout 包住。
              executor.cancel 內部會 await 30s subprocess grace;shutdown window
              內不能等滿 (會超過 container termination grace period),所以只給
              cancel_timeout 秒 —— SIGTERM 在 timeout 前已送出,subprocess 收到
              訊號,真正回收交給 container teardown / OS。

    與 W5 的關係:orphan 偵測是最後保底。即使本函式來不及做完 (硬殺 / 逾時),
    其他 instance 的 check 路徑仍會在 ORPHAN_TIMEOUT_SEC 後標記。本函式讓
    「中斷」即時且準確記錄,而非等 90s 推定。

    明確不做:對已綁 Teams 的 job 在此觸發中斷通知 —— shutdown window 內再打
    外部 webhook 會增加逾時風險;使用者下次 check_pending_tasks 會收到中斷回報。

    Args:
        reason: 中斷原因 (寫入 job.error,供 audit)。
        cancel_timeout: 單一 executor.cancel 的最長等待秒數 (best-effort SIGTERM)。

    Returns:
        處理的 job 數 (供 log / 測試)。
    """
    if not _inflight_jobs:
        return 0

    # 快照後再迭代:Pass 2 的 executor.cancel 可能間接觸發 bg task 收尾 →
    # _deregister_inflight 改動 dict,迭代原 dict 會出錯。
    snapshot = list(_inflight_jobs.items())
    logger.warning(
        f"[shutdown] marking {len(snapshot)} in-flight job(s) INTERRUPTED "
        f"(reason={reason}, replica={_replica_id})"
    )

    # ── Pass 1:標 Table INTERRUPTED (durable,front-load) ──
    for job_id, session_id in snapshot:
        if _job_store is not None:
            try:
                await _job_store.update(
                    session_id, job_id,
                    status=JobStatus.INTERRUPTED.value,
                    error=f"replica 關閉中斷 ({reason})",
                )
            except Exception as e:
                logger.error(
                    f"[shutdown] failed to mark job={job_id} INTERRUPTED: {e}"
                )

    # ── Pass 2:best-effort SIGTERM 本地 subprocess (短逾時,不等完整 grace) ──
    for job_id, session_id in snapshot:
        if _executor is not None:
            try:
                await asyncio.wait_for(
                    _executor.cancel(session_id), timeout=cancel_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[shutdown] executor.cancel timed out for "
                    f"session={session_id} (SIGTERM already sent, container "
                    f"teardown will reap)"
                )
            except Exception as e:
                logger.warning(
                    f"[shutdown] executor.cancel failed for "
                    f"session={session_id}: {e}"
                )

    return len(snapshot)


async def _job_companion_loop(session_id: str, job_id: str) -> None:
    """W4 + W5 owner 端伴隨迴圈:與 bg task 的 _workflow.run 並行。

    2026.06.12 George : max_replicas>1 — 整案唯一新元件。

    一個迴圈做兩件事 (主文件 §5.2「二合一」):
      - W5 heartbeat:每 HEARTBEAT_INTERVAL 秒 MERGE last_heartbeat 到 Table,
        讓其他 instance 的 orphan 偵測知道本 job 還活著。
      - W4 cancel 輪詢:每 tick 讀 cancel_requested;為真則對本地 subprocess
        發 executor.cancel (SIGTERM→grace→SIGKILL)。這是「cancel 打到 instance B、
        job 跑在 A」時,A 端真正把 subprocess 砍掉的執行者 (cancel_pending_task
        的本地快路徑只能砍同 instance 的 subprocess)。

    生命週期:由 _run_coding_agent_inner 在 _workflow.run 前 create_task,run
    結束 (正常 / 例外) 時 cancel 掉。被 cancel 時 asyncio.sleep 拋 CancelledError,
    迴圈自然退出,無資源需釋放。

    cancel 已發出後:停止重發 (cancel_issued 旗標),但 **繼續 heartbeat** —— 因
    subprocess 可能要 30 秒 grace 才真正結束,期間仍需 heartbeat 避免被誤判孤兒。
    終態標記 (CANCELLED) 由 _run_coding_agent_inner 在 run 返回後處理,不在此。

    所有 Table 操作都 fail-safe (heartbeat / is_cancelled 內部已容錯,這裡再包
    一層 try),單次失敗只 log 不中斷迴圈 —— companion 掛掉會讓 heartbeat 停、
    job 被誤判孤兒。
    """
    last_hb = 0.0  # 0 → 第一圈立即寫一次 heartbeat
    cancel_issued = False
    while True:
        now = time.monotonic()

        # ── W5 heartbeat ──
        if now - last_hb >= HEARTBEAT_INTERVAL:
            try:
                await _job_store.heartbeat(
                    session_id, job_id, replica_id=_replica_id
                )
                last_hb = now
            except Exception as e:
                logger.warning(
                    f"[companion] heartbeat failed job={job_id}: {e}"
                )

        # ── W4 cancel 輪詢 ──
        if not cancel_issued:
            try:
                if await _job_store.is_cancelled(session_id, job_id):
                    logger.info(
                        f"[companion] cancel flag detected for job={job_id}, "
                        f"cancelling local executor (session={session_id})"
                    )
                    if _executor is not None:
                        await _executor.cancel(session_id)
                    cancel_issued = True
            except Exception as e:
                logger.warning(
                    f"[companion] cancel poll failed job={job_id}: {e}"
                )

        await asyncio.sleep(CANCEL_POLL_INTERVAL)


async def _run_coding_agent_inner(
    user_input: str,
    session_id_hint: Optional[str],
    credentials: Optional[dict],
    job_id: Optional[str],
    # 2026.08.31 George : route_only — 從 run_workflow 透傳,寫進 state.mode 供
    # turn loop 短路點與保險絲判斷。預設值讓既有 fallback 路徑不用改。
    mode: str = "execute",
    # 2026.09.04 George : scenario 收斂 — 從 run_workflow 透傳,寫進 state.scenario。
    scenario: str = "",
) -> dict:
    """
    Coding Agent 的核心執行函式 (Phase 0 v1.6 run_workflow 主體搬進來)。

    2026.05.18 George : v1.7 Phase 3 抽出此函式
    把 v1.6 run_workflow 的主體 (line 325-449) 整段搬進來,
    新增完成路徑的 Job Store 終態更新 + _deliver_result_or_notify 通知分流。

    執行邏輯 100% 保留 (從 create_new_state 到 save_metadata,流程不變):
    1. 建 state、注入 credentials、OBO exchange
    2. 從 Table Storage 載入 metadata、_apply_metadata_to_state
    3. _inject_session_context (turn history + recent_full_outputs + final_code)
    4. await _workflow.run(user_input, state) 跑 coding agent loop
    5. 累積 turn_history、更新 recent_full_outputs
    6. save_metadata
    7. 回傳 result dict

    Phase 3 新增的尾巴邏輯:
    8. (若 job_id 非 None) Job Store 更新終態 + result JSON 存 result 欄位
    9. (若 job_id 非 None) _deliver_result_or_notify 走 waiter 或 Teams 通知

    Args:
        user_input: 使用者請求文字
        session_id_hint: 外殼決定的 effective session_id (從外面傳進來,
                        確保跟 Job Store entity 的 PartitionKey 一致)
        credentials: 含 __user_token 的 dict,OBO exchange 在內部進行
        job_id: 對應的 Job ID。None 表示 fallback 路徑 (沒有 _job_store
               時的直接同步呼叫),這條路徑下不更新 Job Store,也不走
               通知分流,行為等同 Phase 0 v1.6 的 run_workflow。

    Returns:
        result dict — 與 Phase 0 v1.6 run_workflow 回傳格式相同。
    """
    # 用 try/except 包整段,確保任何例外都會更新 Job Store 為 failed
    # (否則 bg task 拋例外 detach 後,Job Store 會永遠 stuck 在 running)
    try:
        # ─────────────────────────────────────────────────────────
        # 以下整段邏輯與 Phase 0 v1.6 run_workflow 主體相同,除了:
        # - session_id 來自 session_id_hint (外殼算好的 effective)
        # - 結尾新增 job_id 完成路徑
        # ─────────────────────────────────────────────────────────

        # 1. 建立新的 state
        state = create_new_state()

        # 注入 credentials 為環境變數(如果有的話)
        # 2026.03.17 George: v1.2 Identity Passthrough — OBO exchange
        if credentials:
            # 提取 user token(由 mcp_server.py 注入的 __user_token)
            user_token = credentials.pop("__user_token", None)
            # OBO 成功與否 = 這張 token 有沒有被 Entra 驗過,是下方注入身分的唯一依據
            obo_verified = False
            if user_token:
                # 2026.06.09 George: Routine fire-time 分流
                # Routine 觸發時,Foundry 帶進來的是 agent identity token
                # (app-only,無 user subject),走 OBO 會失敗(OnBehalfOfCredential
                # 需要 user assertion)。依 token 類型分流:
                #   - user delegated → 照舊走 OBO 三層交換(互動路徑,不變)
                #   - app / agent    → 跳過 OBO,用 ACA MI 直接取 Azure SQL token,
                #                      注入 registry 中對應 SQL 的 env var,讓
                #                      dynamic skills gate 仍查得到 SQL。
                if is_user_delegated_token(user_token):
                    try:
                        obo_tokens = await obo_exchange_all(user_token)
                        if obo_tokens:
                            obo_verified = True
                            state.user_data.update(obo_tokens)
                            logger.info(
                                f"[OBO] Injected {len(obo_tokens)} resource tokens: "
                                f"{list(obo_tokens.keys())}"
                            )
                    except ValueError as e:
                        # OBO 設定不完整(缺 env vars)— log warning 但不阻斷
                        logger.warning(f"[OBO] Skipped (config incomplete): {e}")
                    except Exception as e:
                        logger.error(f"[OBO] Unexpected error during exchange: {e}")
                else:
                    # ── App / Agent context(Routine fire-time)──
                    # 沒有 user 可以 OBO。改用 ACA MI 取 SQL token,身分為 ACA MI
                    # ([aca-app-name]),fire-time SUSER_SNAME() 即為此 MI。
                    #
                    # ⚠️ Demo 取捨:此 MI 在 RLS 上帶 publish 路徑的
                    #    SUSER_SNAME()='[aca-app-name]' bypass,因此會撈到「全部」
                    #    skills,不是 scoped 子集。Demo 階段以 agent 層正面表列控管。
                    # TODO(正式,二選一):
                    #    (a) Routine 改掛『另一顆無 bypass 的 routine UAMI』,
                    #        _get_admin_sql_token 換成用該 UAMI 的 client_id;或
                    #    (b) 在 code 層對撈回的 skills 做 allowlist intersect
                    #        (見下游 skills_factory 注入點),硬性只留准用 skill。
                    logger.info(
                        "[OBO] App/agent token detected (no user subject) — "
                        "skipping OBO; acquiring Azure SQL token via ACA MI "
                        "(routine path)"
                    )
                    try:
                        sql_token = await _get_admin_sql_token()
                        # 從同一份 OBO_SCOPE_REGISTRY 反查 SQL 的 env var 名,
                        # 避免與 registry 定義漂移(找不到時 fallback 預設名)。
                        sql_env = resolve_env_name_for_scope(
                            "database.windows.net",
                            default="AZURE_SQL_ACCESS_TOKEN",
                        )
                        state.user_data[sql_env] = sql_token
                        logger.info(
                            f"[OBO] Injected {sql_env} via ACA MI (routine path)"
                        )
                    except Exception as e:
                        logger.error(
                            f"[OBO] Failed to acquire SQL token via MI "
                            f"(routine path): {e}"
                        )

            # 2026.09.04 George : 保留前綴清洗 —— 必須在 merge 之前
            # 呼叫端不得自稱身分。順序是本段唯一的安全性關鍵:
            #   剔除呼叫端的 EAA_VERIFIED_* → merge credentials → 最後才寫我方的值。
            # 顛倒任一環,呼叫端就能靠 last-write-wins 冒充任意使用者。
            forged_keys = [
                k for k in credentials if k.startswith(EAA_VERIFIED_PREFIX)
            ]
            for key in forged_keys:
                credentials.pop(key, None)
            if forged_keys:
                logger.warning(
                    f"[Identity] Stripped {len(forged_keys)} caller-supplied "
                    f"reserved key(s) from credentials: {forged_keys}"
                )

            # 剩餘的 credentials 照舊注入(向後相容)
            if credentials:
                state.user_data.update(credentials)
                logger.info(f"[State] Injected {len(credentials)} credentials as env vars")

            # 2026.09.04 George : 注入已驗證身分
            # 取信的依據是 OBO 成功 —— Entra 在交換時已驗過簽章 / aud / exp /
            # 交換資格,偽造的 token 換不到下游 token,所以本地不需要再驗一次簽。
            # ⚠️ 反過來說:日後若拿掉或繞過 OBO,這個變數的可信度會一併消失。
            # OBO 沒跑 / 失敗 / 取不到 claim → 不建立 key,讓 skill 端 KeyError
            # 中止,而不是拿到空字串繼續往下做。
            if obo_verified:
                verified_upn = extract_verified_upn(user_token)
                if verified_upn:
                    state.user_data[VERIFIED_USER_UPN_ENV] = verified_upn
                    logger.info(
                        f"[Identity] Injected {VERIFIED_USER_UPN_ENV} "
                        f"(source: OBO-verified token)"
                    )
                else:
                    logger.warning(
                        f"[Identity] OBO succeeded but no UPN claim resolved — "
                        f"{VERIFIED_USER_UPN_ENV} not injected"
                    )

        # 2. 從 Table Storage 載入 metadata
        # session_id_hint 是外殼算好的 effective_session_id (Phase 3 新增)。
        # 若為 None (fallback 路徑),behavior 同 Phase 0 v1.6 — 不載入。
        metadata = None
        if _conv_store and session_id_hint:
            metadata = await _conv_store.load_metadata(session_id_hint)
            if metadata:
                _apply_metadata_to_state(state, metadata)
                logger.info(
                    f"[State] Loaded metadata: session={session_id_hint}, "
                    f"exec_count={metadata.get('execution_count', 0)}, "
                    f"turns={len(metadata.get('turn_history', []))}, "
                    f"recent_full_outputs={len(metadata.get('recent_full_outputs', {}))}"
                )

        # state.session_id 此時若有 metadata 已被還原,沒有則仍是
        # create_new_state() 生的新 uuid。effective_session_id 用於後續
        # 寫回 Table 的 RowKey。
        effective_session_id = session_id_hint or state.session_id

        # 2.5 (Phase 3 新增) 同步 state.session_id 與 effective_session_id
        # 若 Phase 0 v1.5 修正後 state.session_id 已經跟 effective 一致 (有
        # metadata 還原),這行是 no-op。沒 metadata 還原時 (e.g. 新 session),
        # 強制把 state.session_id 設為外殼決定的 effective,確保
        # executor.execute(session_id=state.session_id, ...) 跟 Job Store
        # PartitionKey 一致,以及 cancel 路徑能找對 proc。
        if session_id_hint:
            state.session_id = session_id_hint

        # 2.7 (Phase 3 新增) 把 job_id 寫進 state,讓 code_agent_hosted.py
        # 的 turn loop 可以在每個 turn 開始前檢查 cancel flag。
        # state.job_id 是 Phase 3 新增的 attribute (ConversationState 需要
        # 新增此欄位,預設 None)。fallback 路徑下 job_id=None。
        state.job_id = job_id

        # 2.8 (2026.08.31 George) route_only — 把 mode 寫進 state。
        # 必須在 _workflow.run() 之前,turn loop 第一輪就要讀得到。
        state.mode = mode

        # 2.9 (2026.09.04 George) scenario 收斂 — 同上,還要趕在 per-turn
        # SkillsProvider 建立之前。
        state.scenario = scenario

        # 3. 注入 session context
        if metadata:
            _inject_session_context(state, metadata)

        # 4. 執行 workflow(workflow.run 內部會 add_user_message)
        # 2026.06.12 George : max_replicas>1 — W4+W5 owner 端伴隨迴圈
        # 在 _workflow.run 期間並行跑 heartbeat 寫入 (W5) + cancel flag 輪詢 (W4)。
        # 只在真實 adaptive-timeout 路徑啟動 (有 job_id + _job_store);dev/test
        # fallback (job_id=None) 不啟動,行為與改動前完全一致。
        # 登記簿 (W5/W6) 與 companion 同生命週期,精確對齊「本 replica 有
        # subprocess 在跑」,供 W6 SIGTERM 列舉。
        _companion = None
        if job_id is not None and _job_store is not None:
            _register_inflight(job_id, effective_session_id)
            _companion = asyncio.create_task(
                _job_companion_loop(effective_session_id, job_id),
                name=f"companion-{job_id[:8]}",
            )
        try:
            result = await _workflow.run(user_input, state)
        finally:
            if _companion is not None:
                _companion.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await _companion
                _deregister_inflight(job_id)

        # 4.4 (2026.07.20 George) token usage 收尾 — 單一 accumulator、單次寫入。
        # this_turn  = 本次 invocation 的用量(state.usage_totals 由 _call_agent 累加)。
        #              放進 result["usage"] → 隨 result JSON 進 Job Store,也進本輪 turn summary。
        # cumulative = 上一輪 metadata 的 session 累計 + 本 turn → 寫回 state.session_usage_totals,
        #              由 save_metadata 在同一次 upsert 寫成 usage_json(與 turn_history 同源同寫,零 drift)。
        _usage_keys = ("input_token_count", "output_token_count", "total_token_count")
        this_turn_usage = {
            k: int(getattr(state, "usage_totals", {}).get(k, 0) or 0)
            for k in _usage_keys
        }
        result["usage"] = this_turn_usage
        _prev_usage = (metadata or {}).get("usage_totals", {}) or {}
        state.session_usage_totals = {
            k: int(_prev_usage.get(k, 0) or 0) + this_turn_usage[k]
            for k in _usage_keys
        }
        logger.info(
            f"[Usage] turn input={this_turn_usage['input_token_count']}, "
            f"output={this_turn_usage['output_token_count']}, "
            f"total={this_turn_usage['total_token_count']}; "
            f"session total={state.session_usage_totals['total_token_count']}"
        )

        # 4.5 生成 turn summary 並累積到 turn_history
        turn_history = (metadata or {}).get("turn_history", [])
        new_turn = _build_turn_summary(
            turn_number=len(turn_history) + 1,
            user_request=user_input,
            result=result,
            state=state,
        )
        turn_history.append(new_turn)

        # 控制 history 長度:保留第一輪 + 最近 N-1 輪
        if len(turn_history) > MAX_TURN_HISTORY:
            turn_history = [turn_history[0]] + turn_history[-(MAX_TURN_HISTORY - 1):]
            logger.info(
                f"[State] turn_history trimmed to {len(turn_history)} turns "
                f"(max={MAX_TURN_HISTORY})"
            )

        state.turn_history = turn_history

        # 4.6 更新 recent_full_outputs(N=1 sliding window)
        full_response = result.get("response", "") or ""
        if full_response.strip():
            new_entry = {str(new_turn["turn"]): full_response}

            if RECENT_FULL_OUTPUTS_WINDOW <= 1:
                # N=1: 直接覆蓋(最常見路徑,最省)
                state.recent_full_outputs = new_entry
            else:
                # N>1: 合併舊的 + 新的,保留最近 N 筆
                existing = (metadata or {}).get("recent_full_outputs", {}) or {}
                merged = {**existing, **new_entry}
                # 按 turn index 數值大小排序,保留最新的 N 筆
                sorted_keys = sorted(merged.keys(), key=lambda k: int(k))
                kept_keys = sorted_keys[-RECENT_FULL_OUTPUTS_WINDOW:]
                state.recent_full_outputs = {k: merged[k] for k in kept_keys}

            logger.info(
                f"[State] recent_full_outputs updated: "
                f"turn={new_turn['turn']}, size={len(full_response)} chars, "
                f"window={RECENT_FULL_OUTPUTS_WINDOW}"
            )
        else:
            # 本輪沒有有效輸出,清空(避免保留過期資料誤導下一輪)
            state.recent_full_outputs = {}

        # 5. 儲存 metadata 到 Table Storage
        # 2026.08.31 George : route_only 不落 metadata。呼叫端(skill generator 的
        # 路由驗證)每個樣本用獨立 session 且永不回帶,寫了沒人讀;批次跑數百個樣本
        # 會在 Table 累積等量垃圾。也讓「route_only 不寫入」這條不變式少一個例外。
        if _conv_store and mode != "route_only":
            await _conv_store.save_metadata(effective_session_id, state)

        # 6. 在 result 中帶回 session_id(供 caller 下輪帶回)
        result["session_id"] = effective_session_id
        result["skills_referenced"] = state.skills_referenced

        # ─────────────────────────────────────────────────────────
        # 7. (Phase 3 新增) Cancel 檢查 + Job Store 終態 + 通知分流
        # ─────────────────────────────────────────────────────────
        if job_id is not None and _job_store is not None:
            # 7a. 檢查在 bg task 跑期間是否被 cancel
            #
            # core_handler.run_workflow 內無法在 turn 邊界檢查 cancel flag,
            # 那部分要由 code_agent_hosted.py 的 turn loop 處理。但在這裡
            # bg task 收尾時可以再檢查一次:若使用者在 task 跑到一半 cancel,
            # 且 turn loop 已經 break 出來,本檢查可以把 Job Store 標為
            # cancelled 而非 completed。
            cancelled = await _job_store.is_cancelled(effective_session_id, job_id)

            if cancelled:
                logger.info(
                    f"[Job {job_id}] Detected cancel flag at completion, "
                    f"marking as cancelled (not completed)"
                )
                await _job_store.update(
                    effective_session_id, job_id,
                    status=JobStatus.CANCELLED.value,
                )
                # 不走 _deliver_result_or_notify (主文件 §5.7.2:cancelled
                # 不推 Teams 通知,使用者已主動取消)。但若同步 waiter 仍在
                # 等,還是要把結果交付給他 (使用者 cancel 後又留在 thread
                # 的場景)。
                if await _job_state_store.has_waiter(job_id):
                    # 給 waiter 一個 cancelled shape 的結果,讓他知道任務
                    # 是被取消而非自然完成
                    cancelled_result = {
                        "status": "cancelled",
                        "session_id": effective_session_id,
                        "job_id": job_id,
                        "message": "任務已取消",
                    }
                    await _job_state_store.set_result(job_id, cancelled_result)
                    # 2026.05.19 v1.9: cancelled 也算已交付給 waiter,標 picked_up
                    # 避免下輪同 session 撈到這筆 cancelled 的舊 job
                    await _mark_result_picked_up(effective_session_id, job_id)
                return result  # bg task 收尾退出

            # 7b. 正常完成 — 寫終態到 Job Store
            success = result.get("success", False)
            terminal_status = (
                JobStatus.COMPLETED.value if success else JobStatus.FAILED.value
            )

            # 嘗試把 result serialize 進 Job Store 的 Result 欄位,失敗也不阻斷
            # 2026.05.19 George : v1.9 — json 已移到檔頭 import,刪 inline
            try:
                result_json = json.dumps(result, ensure_ascii=False, default=str)
                # Table Storage 字串欄位有 64KB 上限,超過就只存標記
                if len(result_json) > 60_000:
                    result_json = (
                        '{"_note": "result too large to persist in Job Store, '
                        'see _conv_store metadata for details"}'
                    )
            except Exception as e:
                logger.warning(
                    f"[Job {job_id}] Failed to serialize result: {e}"
                )
                result_json = None

            update_fields = {"status": terminal_status}
            if result_json:
                update_fields["result"] = result_json
            if not success:
                # error 訊息存 stderr / response 的 fallback
                err_msg = result.get("response", "") or "(no error message)"
                update_fields["error"] = err_msg[:5000]  # 截一下避免太大

            try:
                await _job_store.update(
                    effective_session_id, job_id,
                    **update_fields,
                )
            except Exception as e:
                # 寫終態失敗不該阻斷通知 — log 並繼續走 _deliver_result_or_notify
                logger.error(
                    f"[Job {job_id}] Failed to update Job Store terminal "
                    f"state: {e}",
                    exc_info=True,
                )

            # 7c. 通知分流 (主文件 §3.5)
            # 2026.05.19 v1.9: 加 session_id 讓 picked_up 標記能寫回 Table Storage
            await _deliver_result_or_notify(
                job_id=job_id,
                session_id=effective_session_id,
                result=result,
                is_failure=(not success),
            )

        return result

    except Exception as e:
        # bg task 拋例外 — 必須更新 Job Store,避免 stuck 在 running
        logger.error(
            f"[Job {job_id}] Coding agent raised exception: {type(e).__name__}: {e}",
            exc_info=True,
        )

        # 構造 failure result
        failure_result = {
            "success": False,
            "response": f"任務執行失敗:{type(e).__name__}: {e}",
            "session_id": session_id_hint,
            "skills_referenced": [],
            "uploads": [],
        }

        if job_id is not None and _job_store is not None:
            try:
                await _job_store.update(
                    session_id_hint, job_id,
                    status=JobStatus.FAILED.value,
                    error=f"{type(e).__name__}: {e}"[:5000],
                )
            except Exception as e2:
                logger.error(
                    f"[Job {job_id}] Failed to update Job Store after "
                    f"exception: {e2}"
                )

            # 失敗也要通知 — 主文件 §8.3「失敗通知比成功通知更重要」
            # 2026.05.19 v1.9: 加 session_id 讓 picked_up 標記能寫回 Table Storage
            await _deliver_result_or_notify(
                job_id=job_id,
                session_id=session_id_hint,
                result=failure_result,
                is_failure=True,
            )

        return failure_result


async def _deliver_result_or_notify(
    job_id: str,
    session_id: str,
    result: dict,
    is_failure: bool,
) -> None:
    """
    Bg task 完成後決定走 in-memory waiter 還是 Teams 通知。

    對應主文件 v3 §3.5 設計:
    1. 檢查 waiter — 有就 set_result 走同步路徑 (run_workflow 的 80 秒窗還在等)
    2. 沒有 → 等 grace period (3 秒)
    3. 回讀 Table 確認沒被別的 instance 領走,才推 Teams

    Phase 1 驗證 4 已通過此邏輯,baseline 在 phase1_race_test.py。

    2026.08.15 George : 移除 grace 後重檢 waiter 的 grace-recovered 分支。
    W2 (2026.06.12) 後 check_pending_tasks 改查表、不再 register_waiter,而
    register_waiter 只有 run_workflow 呼叫且在 finally 清掉 → detach 後不可能
    有新 waiter 出現,該分支恆不可達。grace sleep 保留,作用改為「給查表的
    checker 標 picked_up 的時間」,支撐下方的 Teams 去重。

    2026.05.19 George : v1.9 — 加 session_id 參數 + picked_up 標記
    -----------------------------------------------------------
    走 set_result (in-memory event) 路徑 = 結果已透過同步通道交付給
    check_pending_tasks 的 waiter,等同已被使用者領取 → 標 picked_up=True,
    避免下一輪同 session 的 check_pending_tasks 又把這筆舊 job 撈回來。
    走 Teams 通知路徑 = 沒人在等,結果只存在 Job Store,等使用者下次來查 →
    picked_up 維持 False,讓 check_pending_tasks 走終態分流回傳。

    對應 v1.9 P0 修正:解決同一 session 跨輪 retry 時,新任務的 RUNNING
    被舊任務的 COMPLETED-未領取搶先撈到的 race。

    Args:
        job_id: 對應的 Job ID
        session_id: 對應的 session ID (PartitionKey,寫 picked_up 標記用)
        result: 要交付的結果 (run_coding_agent_inner 的回傳)
        is_failure: True 表示是 failure case,Teams 通知會用失敗模板
    """
    # 第一次檢查 — 同步路徑
    if await _job_state_store.has_waiter(job_id):
        logger.info(
            f"[Notify] Job {job_id} has active waiter, delivering via "
            f"in-memory event (sync path)"
        )
        await _job_state_store.set_result(job_id, result)
        # 2026.05.19 v1.9: 走 in-memory event = 已交付給 waiter = 已領取
        await _mark_result_picked_up(session_id, job_id)
        return

    # 沒人在等 — 等一個 grace period,給查表 long-poll 的 checker 標 picked_up
    logger.info(
        f"[Notify] Job {job_id} has no waiter, entering grace period "
        f"({GRACE_PERIOD_SECONDS}s) before Teams notification"
    )
    await asyncio.sleep(GRACE_PERIOD_SECONDS)

    # 2026.06.12 George : max_replicas>1 — Teams 去重 (純查表,Table 為 SoR)
    # W2 後,使用者的 check_pending_tasks 改走查表 long-poll,可能在「另一個
    # instance」上看到本 job 終態、交付結果並標 picked_up=True。此時本 instance
    # 的 has_waiter 看不到那個 waiter (跨 process),grace 後仍會走到這裡。
    # 發 Teams 前回讀一次 Table:若已被別處交付 (picked_up=True) 則跳過,避免
    # 使用者同一答案收兩次 (check 回傳 + Teams 卡)。
    # 時序:grace=3s > CHECK_POLL_INTERVAL=2s,checker 至少輪詢一次標掉 picked_up。
    # 容錯:回讀失敗不阻斷通知 — 寧可重複通知也不要漏通知。
    if _job_store is not None:
        try:
            latest = await _job_store.get(session_id, job_id)
            if latest is not None and latest.result_picked_up:
                logger.info(
                    f"[Notify] Job {job_id} already delivered via check "
                    f"(picked_up=True), skipping Teams notification"
                )
                return
        except Exception as e:
            logger.warning(
                f"[Notify] picked_up re-read failed for job={job_id}, "
                f"proceeding with Teams notification: {e}"
            )

    # 真的沒人在等 — Teams 通知
    # 2026.05.18 George : v1.8 Phase 4 — 換到真實 notifier
    # 2026.05.19 v1.9: 推 Teams 通知時 picked_up 保持 False,
    # 等使用者回 thread 觸發 check_pending_tasks 終態分流時才標記。
    await _send_teams_notification(
        job_id=job_id,
        result=result,
        is_failure=is_failure,
    )


async def _mark_result_picked_up(session_id: str, job_id: str) -> None:
    """標記 job 的 result_picked_up=True,失敗只 log 不阻斷主流程。

    2026.05.19 George : v1.9 — P0 修 Adaptive Timeout 領取漏洞。

    呼叫時機:
    - _deliver_result_or_notify 走 set_result 路徑後 (in-memory event 已交付)
    - _deliver_terminal_job 取回終態 result 後 (Table Storage 直接讀取)

    寫入失敗 (Storage 短暫故障 / 網路斷線) 的容錯:
    - 主流程已經把結果交給使用者,標記只是為了避免下次同 session 撈到舊 job
    - 失敗時 log warning,下次同 session 來查還是會撈到這筆 (走終態分流再領
      一次,使用者多看一次同樣結果不致命,比拋例外炸掉好)
    """
    if _job_store is None:
        return
    try:
        await _job_store.update(
            session_id, job_id,
            result_picked_up=True,
        )
    except Exception as e:
        logger.warning(
            f"[_mark_result_picked_up] Failed to mark job={job_id} "
            f"in session={session_id}: {e}"
        )


async def _send_teams_notification(
    job_id: str,
    result: dict,
    is_failure: bool,
) -> None:
    """
    Teams 通知 — 從 Job Store 拉 user identity,fire-and-forget POST 到
    Logic App webhook。

    2026.05.18 George : v1.8 Phase 4
    取代 Phase 3 的只-log 實作。Phase 4 範圍:
    - 從 Job Store 拉 user_email / user_oid / task_description
      (create 時已寫入,這裡是唯一可靠來源 — 因為 exception 路徑下
       state 可能根本沒建立完整)
    - 組 webhook payload
    - fire-and-forget asyncio.create_task 發送 (不 await,bg task 立即釋放)
    - exponential backoff 重試 1s/2s/4s (主文件 §8 失敗策略)

    一致性語意:best-effort 通知。任務狀態以 Job Store 為準,通知遺失時
    使用者仍可在原 thread 內主動問 check_pending_tasks 取得結果。
    這個取捨在 Phase 1.5 完成 Hosted Agent 搬遷後會有更好的解法,
    本階段不做 persistent queue。

    Args:
        job_id: 對應的 Job ID
        result: 要顯示的結果 (run_coding_agent_inner 回傳)
        is_failure: True 表示是失敗情況
    """
    session_id = result.get("session_id", "")

    if not TEAMS_NOTIFY_WEBHOOK_URL:
        # 沒設定 webhook → 只 log 不發 HTTP
        kind = "FAILURE" if is_failure else "COMPLETION"
        response_preview = (result.get("response") or "")[:200]
        logger.warning(
            f"[Teams Notification] TEAMS_NOTIFY_WEBHOOK_URL not set, "
            f"falling back to log-only. kind={kind}, job={job_id}, "
            f"session={session_id}, preview={response_preview!r}"
        )
        return

    # ── 從 Job Store 拉 user identity (補篇 §3 — 唯一可靠來源) ──
    user_email: Optional[str] = None
    user_oid: Optional[str] = None
    task_description = ""
    if _job_store is not None:
        try:
            job = await _job_store.get(session_id, job_id)
            if job is not None:
                user_email = job.user_email
                user_oid = job.user_id
                task_description = job.task_description or ""
            else:
                logger.warning(
                    f"[Teams Notification] Job {job_id} not found in Job "
                    f"Store, payload will have null user_email/user_oid"
                )
        except Exception as e:
            # Job Store 讀取失敗不阻斷通知 — 用空欄位送出,Logic App 應有
            # graceful degradation
            logger.error(
                f"[Teams Notification] Job Store read failed for "
                f"job={job_id}: {type(e).__name__}: {e}",
                exc_info=True,
            )

    # ── 組 result_summary (截至 Adaptive Card 容納範圍) ──
    if is_failure:
        result_summary = ""
        error_msg = result.get("response", "") or "未提供錯誤訊息"
        # 錯誤訊息也截一下,避免 Adaptive Card 渲染爆掉
        error_msg = error_msg[:TEAMS_NOTIFY_RESULT_SUMMARY_MAX_CHARS]
    else:
        full_response = result.get("response", "") or ""
        result_summary = full_response[:TEAMS_NOTIFY_RESULT_SUMMARY_MAX_CHARS]
        error_msg = ""

    payload = {
        "job_id": job_id,
        "session_id": session_id,
        "user_email": user_email,
        "user_oid": user_oid,
        "task_description": task_description,
        "status": "failed" if is_failure else "completed",
        "result_summary": result_summary,
        "error": error_msg if is_failure else None,
    }

    logger.info(
        f"[Teams Notification] Scheduling webhook POST for job={job_id}, "
        f"status={payload['status']}, user_email={user_email!r}"
    )

    # ── fire-and-forget:不 await,bg task 立即釋放 ──
    # 重試邏輯在 _post_webhook_with_retry 內。即使重試全失敗也只 log,
    # 不影響 Job Store 終態 (已寫 completed/failed)。
    asyncio.create_task(_post_webhook_with_retry(payload))


async def _post_webhook_with_retry(payload: dict) -> None:
    """
    對 Teams Logic App webhook 發送 POST,失敗時 exponential backoff 重試。

    2026.05.18 George : v1.8 Phase 4

    重試策略 (主文件 §8 失敗策略):
    - 最多 TEAMS_NOTIFY_MAX_ATTEMPTS=4 次嘗試 (1 次首發 + 3 次重試)
    - 重試間隔 1s / 2s / 4s exponential backoff
    - 5xx / 連線錯誤 / timeout → 重試
    - 4xx → 立即放棄 (payload 結構問題,重試也救不回來)

    這個函式跑在 asyncio.create_task 起的 fire-and-forget task 內,
    不被 caller await,失敗不影響 bg task 收尾與 Job Store 終態。

    Args:
        payload: 完整 webhook payload (含 job_id / status / result_summary 等)
    """
    job_id = payload.get("job_id", "(unknown)")
    backoff_seconds = [1, 2, 4]  # 對應第 2/3/4 次嘗試前的等待

    for attempt in range(1, TEAMS_NOTIFY_MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=TEAMS_NOTIFY_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    TEAMS_NOTIFY_WEBHOOK_URL,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )

            # 2xx → 成功,結束
            if 200 <= response.status_code < 300:
                logger.info(
                    f"[Teams Notification] Webhook POST success for "
                    f"job={job_id} on attempt {attempt}/"
                    f"{TEAMS_NOTIFY_MAX_ATTEMPTS} "
                    f"(HTTP {response.status_code})"
                )
                return

            # 4xx (除了 408 / 429) → 立即放棄
            if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                logger.error(
                    f"[Teams Notification] Webhook POST failed with "
                    f"non-retriable {response.status_code} for job={job_id}: "
                    f"{response.text[:500]!r}. Giving up."
                )
                return

            # 5xx / 408 / 429 → 重試
            logger.warning(
                f"[Teams Notification] Webhook POST got HTTP "
                f"{response.status_code} for job={job_id} on attempt "
                f"{attempt}/{TEAMS_NOTIFY_MAX_ATTEMPTS}. "
                f"Response: {response.text[:200]!r}"
            )

        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            # 連線錯誤 / timeout / 對方斷線 → 重試
            logger.warning(
                f"[Teams Notification] Webhook POST network error for "
                f"job={job_id} on attempt {attempt}/"
                f"{TEAMS_NOTIFY_MAX_ATTEMPTS}: {type(e).__name__}: {e}"
            )

        except Exception as e:
            # 預期外的例外 (例如 JSON 序列化問題) → 立即放棄
            logger.error(
                f"[Teams Notification] Webhook POST unexpected error for "
                f"job={job_id}: {type(e).__name__}: {e}. Giving up.",
                exc_info=True,
            )
            return

        # 還沒到最後一次 → 等 backoff 後重試
        if attempt < TEAMS_NOTIFY_MAX_ATTEMPTS:
            wait_seconds = backoff_seconds[attempt - 1]
            logger.info(
                f"[Teams Notification] Retrying job={job_id} after "
                f"{wait_seconds}s backoff..."
            )
            await asyncio.sleep(wait_seconds)

    # 所有重試耗盡 — 只 log,不寫回 Job Store (Q4 決定)
    logger.error(
        f"[Teams Notification] All {TEAMS_NOTIFY_MAX_ATTEMPTS} attempts "
        f"exhausted for job={job_id}. Notification lost. User can still "
        f"retrieve result via check_pending_tasks in the original thread."
    )


# ============================================================================
# Phase 3 NEW: cancel_pending_task / check_pending_tasks
# 2026.05.18 George : v1.7 Phase 3
#
# 對應主文件 v3 §5.3 (check_pending_tasks) / §5.7 (cancel_pending_task)。
# 這兩個函式是 mcp_server.py 的 MCP tool 對應業務邏輯,MCP tool 本身是
# 薄殼,實作集中在這裡。
# ============================================================================


async def cancel_pending_task(session_id: str) -> dict:
    """
    取消指定 session 目前 running 中的 coding job。

    對應主文件 §5.7.2 cooperative cancellation 設計。
    雙路徑取消 (Q4 已敲定):
    1. 寫 Job Store cancel flag — turn loop 下個 iteration 會看到 (跨 turn)
    2. 呼叫 executor.cancel(session_id) — 中斷正在跑的 subprocess (turn 內)

    兩個動作都要做,因為:
    - 若有 subprocess 正在跑 → executor.cancel 立即 SIGTERM → 當前 turn
      execute() 收到非零 returncode 退出 → turn loop 下個 iteration 看到
      cancel flag → break
    - 若 turn 之間沒 subprocess → executor.cancel 回 False (no-op) → turn
      loop 下個 iteration 看到 flag → break

    回傳的是「動作確認」,不等 bg task 真的結束。實際結束時間取決於
    subprocess 是否在 30 秒 grace 內優雅收尾 (詳見 code_executor.py
    cancel())。

    Args:
        session_id: 要取消的 session ID

    Returns:
        三種狀態之一:
        - {"status": "no_running_task", ...} — 找不到 running job
        - {"status": "cancelling", ...} — 已發送取消請求
        - {"status": "error", "error": "..."} — 異常情況
    """
    if _job_store is None:
        return {
            "status": "error",
            "session_id": session_id,
            "error": "Job Store not initialized — Storage credentials missing",
        }

    # 找 running job
    job = await _job_store.find_running_by_session(session_id)
    if job is None:
        return {
            "status": "no_running_task",
            "session_id": session_id,
            "message": "目前沒有進行中的任務",
        }

    job_id = job.job_id
    logger.info(
        f"[cancel_pending_task] session={session_id}, job_id={job_id}, "
        f"task={job.task_description[:50]!r}"
    )

    # 路徑 1:寫 cancel flag (turn 邊界 checkpoint 會看到)
    try:
        await _job_store.update(
            session_id, job_id,
            cancel_requested=True,
        )
    except Exception as e:
        logger.error(
            f"[cancel_pending_task] Failed to write cancel flag for "
            f"job={job_id}: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "session_id": session_id,
            "job_id": job_id,
            "error": f"Failed to write cancel flag: {e}",
        }

    # 路徑 2:呼叫 executor.cancel(session_id) 中斷可能正在跑的 subprocess
    # 即使回 False (沒 proc 在跑) 也 OK,flag 已經寫了,turn loop 會看到
    executor_cancelled = False
    if _executor is not None:
        try:
            executor_cancelled = await _executor.cancel(session_id)
            logger.info(
                f"[cancel_pending_task] executor.cancel returned "
                f"{executor_cancelled} for session={session_id}"
            )
        except Exception as e:
            # executor.cancel 拋例外不該擋住取消流程 — flag 已經寫了
            logger.error(
                f"[cancel_pending_task] executor.cancel raised exception: {e}",
                exc_info=True,
            )

    return {
        "status": "cancelling",
        "session_id": session_id,
        "job_id": job_id,
        "task_description": job.task_description,
        "executor_cancelled": executor_cancelled,
        "message": "已請求取消,正在收尾中",
    }


def _is_terminal_status(status: str) -> bool:
    """check_pending_tasks 視為「可交付終態」的狀態。

    2026.06.12 George : max_replicas>1 — W2 抽出。

    COMPLETED / FAILED / INTERRUPTED 皆可交付給使用者。
    CANCELLED / TIMEOUT 不在此列:使用者已知 cancel 結果;TIMEOUT 是 TTL
    清理的舊孤兒。兩者也都不在 find_recent_unpicked_by_session 的 filter 內,
    本函式實務上不會收到它們,列出僅為語意明確。
    """
    return status in (
        JobStatus.COMPLETED.value,
        JobStatus.FAILED.value,
        JobStatus.INTERRUPTED.value,
    )


async def _deliver_terminal_job(session_id: str, job) -> dict:
    """從終態 job 取回 result、標記 picked_up、投影成 MCP 邊界 shape。

    2026.06.12 George : max_replicas>1 — W2 抽出。

    對應原 v1.9 check_pending_tasks 的終態分流。W2 抽成 helper,
    供兩個呼叫點共用:
    - 初次查詢就已終態 (Teams 通知後使用者回 thread 取結果)
    - 查表 long-poll 期間 RUNNING → 終態的轉換

    COMPLETED / FAILED 行為與原 v1.9 完全一致 (相同訊息、相同 shape)。
    INTERRUPTED (W5 orphan / W6 SIGTERM 造成) 為新增:通常無 result,走 minimal
    shape 並給「被中斷、請重新發起」訊息,與單純 FAILED 區分。
    """
    is_success = (job.status == JobStatus.COMPLETED.value)
    is_interrupted = (job.status == JobStatus.INTERRUPTED.value)

    if job.result:
        try:
            result = json.loads(job.result)
        except json.JSONDecodeError as e:
            logger.error(
                f"[_deliver_terminal_job] Failed to parse stored result "
                f"for job={job.job_id}: {e}"
            )
            # 退而求其次:回最小可用 result,讓使用者至少知道狀態
            result = {
                "success": is_success,
                "response": (
                    job.error
                    or "(任務已完成但結果序列化異常,請聯絡管理員)"
                ),
                "session_id": session_id,
                "skills_referenced": [],
                "uploads": [],
            }
    else:
        # COMPLETED 應有 result;FAILED 可能只有 error;INTERRUPTED 通常無 result
        if is_interrupted:
            default_resp = "任務因系統事件 (部署 / 縮容 / 中斷) 而停止,請重新發起。"
        else:
            default_resp = "(任務已結束但無詳細結果)"
        result = {
            "success": is_success,
            "response": job.error or default_resp,
            "session_id": session_id,
            "skills_referenced": [],
            "uploads": [],
        }

    # 標記已領取,後續同 session 查詢不會再撈到此筆
    await _mark_result_picked_up(session_id, job.job_id)

    # 從 Job Store 還原的 result 是未投影的完整包絡 → 投影成 Helper Agent
    # 該看的最小 shape (剝診斷欄位),與同步路徑同標準。
    return _project_boundary_result(result)


async def check_pending_tasks(
    session_id: str,
    max_wait: int = ADAPTIVE_TIMEOUT_SECONDS,
) -> dict:
    """
    查詢 session 內 running 任務的狀態。若有 running job,進行 long polling
    最多 max_wait 秒等待完成。

    對應主文件 §5.3 設計。Helper Agent 在使用者問「好了嗎」時呼叫此函式。

    語意 (主文件 §5.3.2,2026.05.19 修訂;2026.06.12 W2 改查表 long-poll):
    - 找不到「未結束 / 已結束未領取」的 job → 立即回 no_running_task
    - 找到 COMPLETED / FAILED / INTERRUPTED 且未領取的 job → 直接回 result +
      標記 result_picked_up=True (避免重複領取)
      ⭐ 處理「Adaptive Timeout 已 detach + Teams 通知已發 + 使用者回 thread
         詢問結果」的情境。2026.05.19 P0 修正。
    - 找到 RUNNING job → 查表 long-poll (每 CHECK_POLL_INTERVAL 秒 re-query
      Table Storage),等到終態或 max_wait
      - 轉終態 → 回 result (跟同步路徑同 shape)
      - max_wait 到仍 RUNNING → 回 still_running

    2026.06.12 George : max_replicas>1 — W2
    原本 RUNNING 分支用 in-process asyncio.Event 等待 (register_waiter +
    event.wait())。多 instance 下,job 可能跑在 instance A 而 check 落在
    instance B,B 的 event 永遠不會被 set → 等好等滿回 still_running (bug)。
    改為輪詢 Table Storage (唯一 SoR),任何 instance 都查得到終態。
    run_workflow 的 80 秒同步窗仍用 in-process event (發任務與等待在同一
    request / process,零延遲),不受此改動影響。

    Args:
        session_id: 要查詢的 session ID
        max_wait: 最多等多少秒,預設 80 秒 (對齊 ADAPTIVE_TIMEOUT_SECONDS)

    Returns:
        多種狀態:
        - no_running_task: 沒有未結束 / 已結束未領取的 job
        - completed/failed/...: 正常完成 (走 _build_response_payload 那條 shape)
        - still_running: long polling 也沒等到,告訴使用者再等等
        - error: 異常
    """
    if _job_store is None or _job_state_store is None:
        return {
            "status": "error",
            "session_id": session_id,
            "error": "Job Store not initialized",
        }

    # 2026.06.12 George : max_replicas>1 — W2 查表 long-poll
    # 取代原 in-process asyncio.Event 等待 (見 docstring)。以 Table Storage 為
    # 唯一 SoR 輪詢,涵蓋四種情境並自然合流:
    #   - 初次查詢就已終態未領取 (Teams 通知後使用者回 thread)
    #   - RUNNING → 終態的轉換 (輪詢期間)
    #   - RUNNING 但 owner replica 死亡 → orphan 偵測標 INTERRUPTED 後交付 (W5)
    #   - max_wait 到仍 RUNNING → still_running
    deadline = time.monotonic() + max_wait
    last_job = None
    while True:
        job = await _job_store.find_recent_unpicked_by_session(session_id)
        if job is None:
            # 沒有未結束 / 已結束未領取的 job。
            # 輪詢中途消失 = 可能被 cancel (CANCELLED 不在 unpicked filter 內);
            # 使用者已知狀態,回 no_running_task。
            return {
                "status": "no_running_task",
                "session_id": session_id,
                "message": "目前沒有進行中的任務",
            }

        last_job = job

        # 終態 (COMPLETED / FAILED / INTERRUPTED) → 取回 result + 標記 + 投影
        if _is_terminal_status(job.status):
            logger.info(
                f"[check_pending_tasks] job={job.job_id} terminal "
                f"(status={job.status}) in session={session_id}, delivering"
            )
            return await _deliver_terminal_job(session_id, job)

        # W5 orphan 偵測:RUNNING 但 owner replica 死亡 (heartbeat 逾時)。
        # 惰性掃描 —— 不養常駐 sweeper,在 check 查詢路徑順手判定 (主文件 §5.2)。
        # 標 INTERRUPTED 後當終態交付,使用者收到「任務被中斷,請重新發起」。
        # 明確不做:orphan 自動重跑 (Coding Agent job 不保證冪等,可能已產生
        # 部分副作用,標中斷由使用者決定重發)。
        if _job_store.is_orphaned(job, ORPHAN_TIMEOUT_SEC):
            logger.warning(
                f"[check_pending_tasks] job={job.job_id} orphaned "
                f"(last_heartbeat={job.last_heartbeat!r}, "
                f"timeout={ORPHAN_TIMEOUT_SEC}s) in session={session_id}, "
                f"marking INTERRUPTED"
            )
            orphan_err = "owner replica 失聯 (heartbeat 逾時),任務中斷"
            try:
                await _job_store.update(
                    session_id, job.job_id,
                    status=JobStatus.INTERRUPTED.value,
                    error=orphan_err,
                )
            except Exception as e:
                # 標記失敗不阻斷交付:本地 job 物件已改成 INTERRUPTED,使用者
                # 仍會收到中斷回報;下次 check 會再判一次 orphan 重標。
                logger.error(
                    f"[check_pending_tasks] failed to mark orphan "
                    f"job={job.job_id} as INTERRUPTED: {e}"
                )
            # 本地同步狀態後交付 (避免再讀一次 Table)
            job.status = JobStatus.INTERRUPTED.value
            if not job.error:
                job.error = orphan_err
            return await _deliver_terminal_job(session_id, job)

        # 還在 RUNNING → 等一個輪詢間隔再查;deadline 到則跳出回 still_running。
        # sleep 夾在 remaining 內,避免超過 caller 的 max_wait (上游可能有
        # APIM / ingress timeout,主文件 §9.2 要求 max_wait ≤ 上游 timeout)。
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(CHECK_POLL_INTERVAL, remaining))

    logger.info(
        f"[check_pending_tasks] job={last_job.job_id} still running after "
        f"{max_wait}s, returning still_running"
    )
    return _build_still_running_response(
        last_job.job_id, session_id, last_job.task_description
    )



def _build_still_running_response(
    job_id: str,
    session_id: str,
    task_description: str,
) -> dict:
    """組「long polling 結束但任務仍在跑」回應。

    與 _build_running_response (主文件 §5.2 detach 情境) 區分:
    - running: run_workflow 發任務後 80 秒未完成,首次告訴使用者
    - still_running: 已等過 80 秒了還沒完成,二次等待結果

    Helper Agent 可以根據 status 不同提供不同的 UX (還在跑 vs 已等了一段
    時間還在跑)。
    """
    return {
        "status": "still_running",
        "job_id": job_id,
        "session_id": session_id,
        "task_description": task_description,
        "message": "任務仍在執行中,你可以稍後再來問或等 Teams 通知",
    }


def _apply_metadata_to_state(state: ConversationState, metadata: dict):
    """將 Table Storage 中的 workflow metadata 套用到新建的 state。"""
    # 2026.04.28 George: v1.5 還原 session_id / work_dir / output_files / execution_count
    # 修正跨輪 session_id 與 work_dir 漂移的 bug:
    #   原本只還原 4 個語意欄位 (hitl_context / original_user_request /
    #   skills_referenced / recent_full_outputs),沒還原 session_id 與
    #   work_dir,導致 create_new_state() 每輪生的新 uuid 直接被當成
    #   state.session_id,連帶 work_dir 變成 /app/session_<新uuid>/
    #   後果:
    #   1. Table entity 裡的 session_id 欄位每輪被覆蓋成新 uuid,
    #      與 RowKey (caller 傳的 conversation session_id) 不一致
    #   2. 同一 conversation 跨輪產檔散落在多個 /app/session_xxx/ 目錄,
    #      CodingAgent 在 turn N+1 想讀 turn N 產的 CSV/PNG 找不到檔案
    #   3. final_script_path 指向舊 work_dir,新 work_dir 裡沒有實體檔案
    # 修正:從 metadata 還原 session_id 與 work_dir,同時 makedirs 確保目錄存在
    # (ACA replica 重啟後 ephemeral storage 會清空,需要重建空目錄)
    stored_session_id = metadata.get("session_id")
    stored_work_dir = metadata.get("work_dir")
    if stored_session_id and stored_work_dir:
        state.session_id = stored_session_id
        state.work_dir = stored_work_dir
        os.makedirs(state.work_dir, exist_ok=True)
        logger.info(
            f"[State] Restored session_id={stored_session_id}, "
            f"work_dir={stored_work_dir}"
        )

    # 還原 workflow 進度 (execution_count 用於 script_v{n}.py 命名,
    # 不還原會導致新一輪寫成 script_v1.py 蓋掉舊檔)
    state.execution_count = metadata.get("execution_count", 0)

    # 還原既有產出檔案清單,過濾掉已不存在於磁碟的 (replica 重啟後可能消失)
    output_files = metadata.get("output_files", []) or []
    state.output_files = [f for f in output_files if os.path.exists(f)]
    if len(state.output_files) < len(output_files):
        logger.info(
            f"[State] Dropped {len(output_files) - len(state.output_files)} "
            f"output_files no longer on disk (likely replica restart)"
        )

    # 既有的 4 個語意欄位
    state.hitl_context = metadata.get("hitl_context")
    state.original_user_request = metadata.get("original_user_request")
    state.skills_referenced = metadata.get("skills_referenced", [])
    # 2026.04.23 George: v1.4 還原 recent_full_outputs
    # 注意:_inject_session_context 實際上是讀 metadata 而非 state.recent_full_outputs,
    # 這裡把值帶回 state 主要是為了 save_metadata 時的一致性
    # (避免本輪跑完後沒有新 output 時,舊值不小心遺失)
    state.recent_full_outputs = metadata.get("recent_full_outputs", {}) or {}


def _inject_session_context(state: ConversationState, metadata: dict):
    """
    2026.03.13 George: 注入完整的 session context。
    取代原本的 _inject_final_code_context,提供 LLM 足夠的跨輪語境。

    注入內容:
    1. turn_history 摘要(前幾輪做了什麼)
    2. original_user_request(最初的任務目標)
    3. recent_full_outputs(上 N 輪的完整輸出,含原始資料)← 2026.04.23 v1.4 新增
    4. final_code(最近一輪的完整程式碼)

    注入機制:透過 state.add_assistant_message(..., "SessionContext")
    把整塊內容當成一則 assistant message 塞進對話歷史,LLM 會自然地
    把它當 context 參考。Helper Agent 已在讀這個 SessionContext message,
    所以 agent instructions 不需要修改。
    """
    parts = []

    # 1. Turn history(讓 LLM 知道前幾輪做了什麼)
    turn_history = metadata.get("turn_history", [])
    if turn_history:
        parts.append("## 前幾輪工作摘要")
        for turn in turn_history:
            turn_num = turn.get("turn", "?")
            user_req = turn.get("user_request", "")
            summary = turn.get("summary", "")
            parts.append(f"- Turn {turn_num}: 用戶要求「{user_req}」→ {summary}")
            output_files = turn.get("output_files", [])
            if output_files:
                parts.append(f"  產出檔案: {', '.join(output_files)}")

    # 2. Original user request(最初的任務目標,提供整體方向感)
    original_req = metadata.get("original_user_request")
    if original_req and turn_history:
        # 只在有多輪歷史時才注入,避免第一輪重複
        parts.append(f"\n## 最初的任務目標\n{original_req}")

    # 3. Recent full outputs(上 N 輪的完整輸出,含原始資料)
    # 2026.04.23 George: v1.4
    # 這塊放在 turn_history 摘要之後、final_code 之前:
    #   - 摘要是「發生了什麼」的索引
    #   - 完整輸出是「具體內容」的資料體
    #   - final_code 是「上一輪跑了哪段程式碼」
    # 三者互補,順序由「概要→資料→程式碼」遞進,符合 LLM 閱讀習慣
    recent_outputs = metadata.get("recent_full_outputs", {}) or {}
    if recent_outputs:
        parts.append("\n## 上一輪的完整輸出(含原始資料,可供本輪重用)")
        # 按 turn index 數值大小升序排列(turn 1, 2, 3...)
        for turn_idx in sorted(recent_outputs.keys(), key=lambda k: int(k)):
            output = recent_outputs[turn_idx]
            parts.append(f"### Turn {turn_idx} 完整輸出")
            parts.append(output)

    # 4. Final code(最近一輪的完整程式碼,供修改用)
    final_code = metadata.get("final_code", "")
    if final_code and final_code.strip():
        parts.append(f"\n## 最近一輪成功執行的程式碼\n```python\n{final_code}\n```")

    if parts:
        context_msg = "\n".join(parts)
        state.add_assistant_message(context_msg, "SessionContext")
        logger.info(
            f"[State] Injected session context: "
            f"{len(turn_history)} turns, "
            f"original_req={'yes' if original_req else 'no'}, "
            f"recent_full_outputs={len(recent_outputs)}, "
            f"final_code={'yes' if final_code else 'no'}"
        )


def _build_turn_summary(
    turn_number: int,
    user_request: str,
    result: dict,
    state: ConversationState,
) -> dict:
    """
    2026.03.13 George: 從 workflow result 中建構本輪的 turn summary。

    Returns:
        {
            "turn": int,
            "user_request": str (截斷至 500 字),
            "summary": str (截斷至 300 字),
            "output_files": list[str],
            "success": bool,
            "timestamp": str (ISO 8601)
        }
    """
    # 從 result 的 response 中提取摘要
    response_text = result.get("response", "")
    success = result.get("success", False)

    if success:
        # 取 response 的前幾行非空行作為摘要
        lines = response_text.strip().split("\n")
        summary_lines = [line.strip() for line in lines[:5] if line.strip()]
        summary = " ".join(summary_lines)[:300]
    else:
        summary = f"執行失敗: {response_text[:200]}"

    return {
        "turn": turn_number,
        "user_request": user_request[:500],
        "summary": summary,
        "output_files": getattr(state, "output_files", []),
        "success": success,
        # 2026.07.20 George : 本輪 token 用量(來自 result["usage"],由 _run_coding_agent_inner 收尾寫入)
        "usage": result.get("usage", {}),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================================
# DEBUG & MANAGEMENT(供各 adapter 的 debug 指令使用)
# ============================================================================
# 2026.04.10 George: change debug_list_skill to list_skills, 讓它成為正式功能的一部分,並且在 sync_skills 後自動列出技能清單,方便驗證同步結果。
#
# 2026.07.05 George : v1.10 — Dynamic Skills(Mode B)身份分流
# 背景:這個 function 當初只掃本地 SKILLS_DIR,完全沒考慮
# DYNAMIC_SKILLS_ENABLED=true 時「本地目錄」跟「這個使用者實際被授權的
# skill」是兩回事——後者只有 per-turn 的 SkillsProviderFactory 依身份查
# v_my_skills(RLS)才知道。
#
# 身份分流(對齊 run_workflow 既有的 is_user_delegated_token 分流,
# 但 app/agent 分支的處理**不一樣**,原因見下方 else 分支註解):
#   - user delegated token → 正常 OBO exchange 拿 SQL token,查 v_my_skills,
#     這時回傳的清單就是這個使用者真正的 RBAC 範圍。
#   - app/agent token(無 user subject)→ 沒有 OBO 可走,只能借用
#     _get_admin_sql_token() 的 ACA MI token。但 MI 在 RLS predicate 上是
#     `SUSER_SNAME() = '[aca-app-name]'` 的顯式 bypass(見 _get_admin_sql_token
#     上方註解),查 v_my_skills 一定拿到「全部」skill,跟
#     user_skill_grants 實際內容無關。run_workflow 借用這把 token 是為了
#     讓 routine 執行「跑得完」,可以接受這個 demo tradeoff;但 list_skills
#     是要「如實告知使用者被授權的範圍」,把 bypass 結果當成 RBAC 清單端
#     出去會誤導人。所以這裡維持能顯示,但在文字上明確標註是 admin
#     bypass 取得的完整目錄,不是 RLS 授權範圍。
#     正式修法待辦(對齊 run_workflow 旁的 TODO):
#       (a) Routine 改掛另一顆無 bypass 的 UAMI,或
#       (b) 在 code 層對撈回的 skills 做 allowlist intersect
#     二選一修完後,這裡的警語文字要一併拿掉。
def _format_skill_file_tree(file_paths: list[str]) -> list[str]:
    """將已過濾的 skill 相對路徑轉成巢狀 Markdown list。"""
    tree: dict = {}
    for file_path in file_paths:
        parts = file_path.replace("\\", "/").split("/")
        node = tree
        for part in parts:
            if part:
                node = node.setdefault(part, {})

    lines: list[str] = []

    def render(node: dict, depth: int) -> None:
        ordered = sorted(
            node.items(),
            key=lambda item: (
                0 if depth == 0 and item[0] == SKILL_ENTRY_FILENAME else 1,
                item[0].casefold(),
                item[0],
            ),
        )
        for name, children in ordered:
            suffix = "/" if children else ""
            lines.append(f"{'  ' * depth}- `{name}{suffix}`")
            if children:
                render(children, depth + 1)

    render(tree, 0)
    return lines


# 2026.08.21 George : Skill Registry 分階 —— parent skill 用 frontmatter
# metadata.children 顯式宣告它會展開哪些 child。
#
# 為什麼綁定資訊放 frontmatter 而不是 SQL:沿用 schema v2 已確立的分工 ——
# SQL 管拓樸與授權(is_internal / grants),Blob SKILL.md 管內容與編排。
# 也因此「is_internal=1 但沒有任何 parent 指向它」這種孤兒無法用純 SQL 對帳,
# 只能在這一層做(見 _detect_orphan_internal_skills)。
def _declared_children(meta: dict) -> list[str]:
    """取出 frontmatter 的 metadata.children;沒有或格式不符時回空 list。"""
    md = meta.get("metadata")
    if not isinstance(md, dict):
        return []
    children = md.get("children")
    if isinstance(children, str):
        children = [children]
    if not isinstance(children, list):
        return []
    return [str(c) for c in children if c]


def _detect_orphan_internal_skills(metas: list[dict]) -> list[str]:
    """
    找出「標了 is_internal 卻沒有任何 skill 的 metadata.children 指向它」的 skill。

    這種 skill 對外目錄看不到、又沒有 parent 會指名載入它 = 永久不可被發現。
    最常見的成因是有人 refine parent 時把 children 寫掉了,child 會靜靜消失,
    不會有任何錯誤。
    """
    declared: set[str] = set()
    for meta in metas:
        declared.update(_declared_children(meta))
    return sorted(
        str(meta.get("name") or "")
        for meta in metas
        if meta.get("_is_internal") and str(meta.get("name") or "") not in declared
    )


def _project_scenario_skills(
    metas: list[dict], *, full_catalog: bool
) -> list[dict]:
    """
    把 internal skill 從對外目錄投影掉,並順手做孤兒對帳。

    ⭐ 過濾只發生在這裡。SQL(v_my_skills)與 runtime materialize
    (SkillsProviderFactory.create_provider_scope)都必須看得到 internal skill,
    否則 parent 正文指名的 child 根本載不進來。

    Args:
        full_catalog: 呼叫端這次拿到的是不是完整目錄。admin bypass 路徑是
            (可信,孤兒判定直接 WARNING);user OBO 路徑不是 —— RLS 可能讓
            使用者看得到 child 卻看不到 parent,會誤報,所以只記 DEBUG。
    """
    orphans = _detect_orphan_internal_skills(metas)
    if orphans:
        if full_catalog:
            logger.warning(
                "[list_skills] orphan internal skill(s) — 標為 internal 但沒有任何 "
                "skill 的 metadata.children 指向它們,將永久不可被發現: %s",
                orphans,
            )
        else:
            logger.debug(
                "[list_skills] possible orphan internal skill(s) (RLS 視野受限,"
                "可能誤報): %s", orphans,
            )

    visible = [m for m in metas if not m.get("_is_internal")]
    hidden = len(metas) - len(visible)
    if hidden:
        logger.info(
            "[list_skills] projection: total=%d visible=%d hidden_internal=%d",
            len(metas), len(visible), hidden,
        )
    return visible


def _format_skills_catalog(header: str, catalog: list[dict]) -> str:
    """格式化 skill frontmatter 與可 materialize 的資料夾結構。"""
    lines = [header, "_Files shown below are the materializable skill contents._\n"]
    for meta in catalog:
        name = meta.get("name", "")
        description = meta.get("description", "")
        lines.append(f"- **{name}**: {description}")

        # 2026.08.21 George : children 提到獨立一行 —— 宿主(DA / Foundry helper)
        # 靠它判斷「這是情境層,要先 fetch_skill 取完整指引」。埋在 metadata 的
        # dict repr 裡會讀不可靠。
        children = _declared_children(meta)
        if children:
            lines.append(f"  - children: {children}")

        for key, value in meta.items():
            if key in ("name", "description") or key.startswith("_"):
                continue
            lines.append(f"  - {key}: {value}")

        lines.append("  - files:")
        file_lines = _format_skill_file_tree(meta.get("_files", []))
        lines.extend(f"    {line}" for line in file_lines)
        if not file_lines:
            lines.append("    - _(none)_")

    return "\n".join(lines)


async def list_skills(credentials: Optional[Dict] = None) -> str:
    """列出目前可用的 Skills,包含名稱與用途描述。

    Dynamic Skills 模式(DYNAMIC_SKILLS_ENABLED=true)下,清單改為即時查
    v_my_skills(RLS-filtered),需要呼叫端把 __user_token 放進 credentials
    (跟 run_workflow 同一個慣例,由 mcp_server.py 注入)。
    Static 模式行為完全不變(掃本地 SKILLS_DIR)。
    """
    # 2026.07.06 George : bugfix — 原本寫 `_dynamic_enabled and _skills_factory
    # is not None` 會炸 NameError
    if _skills_factory is not None:
        user_token = (credentials or {}).get("__user_token")

        if user_token and is_user_delegated_token(user_token):
            # ── 使用者身分:正常 OBO,拿到的清單就是真實 RBAC 範圍 ──
            try:
                obo_tokens = await obo_exchange_all(user_token)
            except Exception as e:
                logger.error(f"[list_skills] OBO exchange failed: {e}")
                return "⚠️ 無法完成身份驗證(OBO exchange 失敗),無法查詢可用 skill 清單。"

            sql_env = resolve_env_name_for_scope(
                "database.windows.net", default="AZURE_SQL_ACCESS_TOKEN"
            )
            sql_token = obo_tokens.get(sql_env)
            if not sql_token:
                return "⚠️ 未取得 Azure SQL OBO token,無法查詢可用 skill 清單(檢查 OBO 設定)。"

            try:
                metas = await _skills_factory.list_allowed_skills_full(sql_token)
            except Exception as e:
                logger.exception(f"[list_skills] SQL query failed: {e}")
                return "⚠️ 查詢 skill 清單時發生錯誤(SQL 連線或 RLS 設定問題),請聯絡管理員。"

            # 2026.07.07 George : debug logging —— 完整 frontmatter 內容已經在
            # SkillsProviderFactory.list_allowed_skills_full() 裡印過,這裡只
            # 補一行「查到哪些 skill 名稱」,方便從 core_handler 這層快速確認
            # RLS 授權範圍對不對,不用跳去翻 factory 層的完整 log。
            logger.info(
                "[list_skills] user OBO path resolved %d skill(s): %s",
                len(metas), [m.get("name") for m in metas],
            )

            if not metas:
                return "⚠️ 目前沒有任何 skill 可用(既未被授權,也沒有公開技能)。"

            # 2026.08.21 George : internal(能力層)skill 在這裡才被投影掉;
            # 上面拿到的 metas 仍是完整集合,runtime 靠它載 child。
            visible = _project_scenario_skills(metas, full_catalog=False)
            if not visible:
                return (
                    "⚠️ 目前沒有任何情境層 skill 可用"
                    "(授權範圍內的 skill 全部是只能由情境層引用的能力層)。"
                )

            return _format_skills_catalog(
                f"## Available Skills ({len(visible)}) — RBAC-filtered\n",
                visible,
            )

        else:
            # ── app/agent context(無 user subject,例如 Routine fire-time)──
            # 見 function 上方大註解:MI token 在 RLS 上是顯式 bypass,
            # 撈到的是「全部」skill,不是這個 app/agent 身份的 scoped 子集。
            # 刻意不擋掉(維持可用),但清楚標註避免誤讀成 RBAC 範圍。
            try:
                sql_token = await _get_admin_sql_token()
            except Exception as e:
                logger.error(f"[list_skills] Failed to acquire admin SQL token: {e}")
                return "⚠️ 無使用者身分,且無法取得 admin SQL token,無法查詢 skill 清單。"

            try:
                metas = await _skills_factory.list_allowed_skills_full(sql_token)
            except Exception as e:
                logger.exception(f"[list_skills] SQL query failed (admin bypass path): {e}")
                return "⚠️ 查詢 skill 清單時發生錯誤(SQL 連線問題),請聯絡管理員。"

            # 2026.07.07 George : debug logging —— 同上,補一行 admin bypass
            # path 查到的 skill 名稱,方便對照是不是真的拿到「全部」skill
            # (這條路徑本來就該是全部,見上方大註解的 MI bypass 說明)。
            logger.info(
                "[list_skills] admin bypass path resolved %d skill(s): %s",
                len(metas), [m.get("name") for m in metas],
            )

            if not metas:
                return "⚠️ Skill 目錄為空。"

            # 2026.08.21 George : 這條路徑拿到的是完整目錄,孤兒判定可信。
            visible = _project_scenario_skills(metas, full_catalog=True)
            if not visible:
                return "⚠️ Skill 目錄中沒有任何情境層 skill(全部被標為 internal)。"

            header = (
                f"## Available Skills ({len(visible)}) "
                "— ⚠️ 透過 admin bypass 取得的完整目錄,非 RLS 授權範圍(app/agent context 無使用者身分)\n"
            )
            return _format_skills_catalog(header, visible)

    # ── Static 模式(原有邏輯,完全不變)──
    # 2026.08.21 George : 刻意不套 is_internal 投影 —— Mode A 掃的是本地
    # SKILLS_DIR,沒有 SQL 這個旗標的來源。分階是 Mode B(正式環境)的能力,
    # Mode A 只是 DYNAMIC_SKILLS_ENABLED=false 的緊急退場路徑,退到「全開」
    # 本來就是它的既有語意。
    import yaml

    if not os.path.exists(SKILLS_DIR):
        return "⚠️ Skills 目錄不存在,可能尚未從 Blob Storage 同步。"

    skill_dirs = sorted(
        e for e in os.listdir(SKILLS_DIR)
        if os.path.isdir(os.path.join(SKILLS_DIR, e))
    )
    if not skill_dirs:
        return "⚠️ Skills 目錄為空(0 個 skill)。"

    catalog = []
    for skill_name in skill_dirs:
        skill_md = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
        meta: Dict = {}
        if os.path.isfile(skill_md):
            try:
                with open(skill_md, "r", encoding="utf-8") as f:
                    content = f.read()
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        parsed = yaml.safe_load(parts[1])
                        if isinstance(parsed, dict):
                            meta = parsed
            except Exception:
                meta = {"description": "(SKILL.md 解析失敗)"}
        else:
            meta = {"description": "(缺少 SKILL.md)"}
        meta.setdefault("name", skill_name)
        meta["_files"] = list_local_skill_files(
            os.path.join(SKILLS_DIR, skill_name)
        )
        catalog.append(meta)

    # 2026.07.07 George : debug logging —— Static 模式(DYNAMIC_SKILLS_ENABLED=false)
    # 對齊 Dynamic 模式(list_allowed_skills_full)的 debug log,方便對照本地
    # SKILLS_DIR 掃到的 frontmatter 內容。同樣只留前 50 + 後 50 字元,避免長
    # description 洗版;用 %r 避免內容有奇怪字元時 log 直接炸掉。
    #
    # 2026.07.07 George : 改遞迴版,跟 skills_provider_factory.list_allowed_skills_full
    # 同步 —— nested dict/list(例如 metadata.call_shape 這種欄位)裡的長字串
    # 也要被截斷,不只最外層 key。
    def _elide(value, head: int = 50, tail: int = 50):
        """字串太長時只保留前 head / 後 tail 字元;dict/list 遞迴處理。"""
        if isinstance(value, str) and len(value) > head + tail:
            return f"{value[:head]}…[{len(value)} chars]…{value[-tail:]}"
        if isinstance(value, dict):
            return {k: _elide(v, head, tail) for k, v in value.items()}
        if isinstance(value, list):
            return [_elide(v, head, tail) for v in value]
        return value

    log_catalog = [{k: _elide(v) for k, v in meta.items()} for meta in catalog]
    logger.info(
        "[list_skills] static mode resolved %d skill(s) from %s: %r",
        len(catalog), SKILLS_DIR, log_catalog,
    )

    return _format_skills_catalog(
        f"## Available Skills ({len(catalog)})\n",
        catalog,
    )


# 2026.08.21 George : Skill Registry 分階配套 —— list_skills 只給 frontmatter,
# 這裡給正文。
#
# 2026.09.06 George : sections 段落投影。mcp_server 的 fetch_skill tool 從一開始
# 就對外開放這個參數,但下游一直沒接 —— 宿主真的傳了就會 TypeError。
_H2_HEADING_RE = re.compile(r"^##(?!#)\s*(.+?)\s*$")


def _normalize_heading(text: str) -> str:
    # 只留字母數字與 CJK,emoji / 反引號 / 標點 / 空白全丟,讓宿主給片段就能命中。
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _split_h2_sections(content: str):
    """切成 [(標題, 該節原文)]。code fence 內的 ## 不算標題。"""
    lines = content.splitlines()
    heads = []
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _H2_HEADING_RE.match(line)
        if m:
            heads.append((i, m.group(1)))
    out = []
    for n, (start, title) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        out.append((title, "\n".join(lines[start:end]).rstrip()))
    return out


def _project_sections(content: str, sections: str):
    """回傳 (投影後正文, 可用節名清單)。無命中時第一個元素為 None。"""
    all_sections = _split_h2_sections(content)
    titles = [t for t, _ in all_sections]
    wanted = [_normalize_heading(s) for s in sections.split(",")]
    wanted = [w for w in wanted if w]
    if not wanted or not all_sections:
        return (None, titles)
    picked = [
        body
        for title, body in all_sections
        if any(w in _normalize_heading(title) for w in wanted)
    ]
    return ("\n\n".join(picked) if picked else None, titles)


# 情境層 parent skill 的內容有兩種讀者:EAA 內部 agent(靠 MAF load_skill 讀
# materialize 到 /tmp 的檔案)與宿主 agent(DA / Foundry helper)。宿主沒有
# 檔案系統,也拿不到 /tmp,而 parent 的編排指引恰好只有宿主能執行 —— 例如
# 「先去查行事曆」這種 EAA 本身沒有權限做的事。沒有這條路徑,那些指引就永遠
# 到不了會執行它的人。
async def fetch_skill(
    skill_name: str, credentials: Optional[Dict] = None, sections: str = ""
) -> str:
    """
    取得單一 skill 的 SKILL.md 完整內容(progressive disclosure 的第二段)。

    權限沿用 list_skills 的同一條 RLS 路徑,不另開後門。internal skill 也取得到
    —— parent 正文會指名 child,擋掉等於製造「叫我去拿卻拿不到」的死路。
    """
    if not skill_name or not skill_name.strip():
        return "⚠️ 請提供 skill_name。"
    skill_name = skill_name.strip()

    if _skills_factory is None:
        # Static 模式沒有 SQL/Blob 來源,而它只是緊急退場路徑,不值得為它
        # 再實作一條本地讀檔分支(本地模式下 skill 全開,宿主也不需要這個)。
        return (
            "⚠️ 目前為 static skills 模式(DYNAMIC_SKILLS_ENABLED=false),"
            "不支援 fetch_skill,請改用 list_skills。"
        )

    user_token = (credentials or {}).get("__user_token")
    if user_token and is_user_delegated_token(user_token):
        try:
            obo_tokens = await obo_exchange_all(user_token)
        except Exception as e:
            logger.error(f"[fetch_skill] OBO exchange failed: {e}")
            return "⚠️ 無法完成身份驗證(OBO exchange 失敗),無法取得 skill 內容。"

        sql_env = resolve_env_name_for_scope(
            "database.windows.net", default="AZURE_SQL_ACCESS_TOKEN"
        )
        sql_token = obo_tokens.get(sql_env)
        if not sql_token:
            return "⚠️ 未取得 Azure SQL OBO token,無法取得 skill 內容(檢查 OBO 設定)。"
    else:
        # 與 list_skills 一致:app/agent context(如 Routine fire-time)無使用者
        # 身分,走 MI admin bypass,此時看得到的是完整目錄而非 RBAC 子集。
        try:
            sql_token = await _get_admin_sql_token()
        except Exception as e:
            logger.error(f"[fetch_skill] Failed to acquire admin SQL token: {e}")
            return "⚠️ 無使用者身分,且無法取得 admin SQL token,無法取得 skill 內容。"

    try:
        content = await _skills_factory.get_skill_content(sql_token, skill_name)
    except Exception as e:
        logger.exception(f"[fetch_skill] failed for {skill_name!r}: {e}")
        return "⚠️ 取得 skill 內容時發生錯誤,請聯絡管理員。"

    if content is None:
        return (
            f"⚠️ 找不到 skill `{skill_name}`,或你沒有使用它的權限。"
            "請先呼叫 list_skills 確認名稱。"
        )

    if sections and sections.strip():
        projected, available = _project_sections(content, sections)
        if projected is None:
            avail = (
                "、".join(f"`{t}`" for t in available)
                if available
                else "(此 skill 沒有 H2 段落,請把 sections 留空)"
            )
            logger.info(
                "[fetch_skill] %s sections=%r no match (avail=%d)",
                skill_name, sections, len(available),
            )
            return (
                f"⚠️ skill `{skill_name}` 找不到符合 `{sections}` 的段落。\n"
                f"可用段落:{avail}\n"
                "請改用其中一個節名,或把 sections 留空取回完整正文。"
            )
        logger.info(
            "[fetch_skill] %s sections=%r resolved (%d/%d chars)",
            skill_name, sections, len(projected), len(content),
        )
        return f"## Skill: {skill_name}\n\n{projected}"

    logger.info("[fetch_skill] %s resolved (%d chars)", skill_name, len(content))
    return f"## Skill: {skill_name}\n\n{content}"


async def sync_skills() -> str:
    global _workflow, _project_client
    try:
        count = await sync_skills_from_blob()
        # 2026.03.17 George: 重建 workflow,讓 FileAgentSkillsProvider 重新讀取 skills
        _workflow, _project_client = await create_workflow(_credential)

        # 2026.05.12 George : v1.6 Phase 0 — 重建後重新注入三個介面
        # 否則 sync 後 _workflow.executor / file_store / job_state_store
        # 會是 None,下一次 run_workflow 直接 AttributeError 或行為錯誤。
        # 這裡重用 startup() 已建立好的 globals (executor / file_store /
        # job_state_store),不重新 new 一份,確保 cancel / job state 等
        # 跨 sync 邊界的內部 state (例如 LocalSubprocessExecutor._running_procs
        # 內可能正在跑的 process refs、_pending_waiters 內未完成的 event)
        # 不會被清掉。
        #
        # 2026.05.18 George : v1.7 Phase 3
        # 注意:JobStore (_job_store) **不** re-inject 到 _workflow,因為
        # 它由 core_handler 直接持有,不掛在 workflow 上 — 設計理由是
        # JobStore 的 caller (run_workflow / cancel_pending_task /
        # check_pending_tasks) 全部在 core_handler 這層,不在 workflow
        # engine 內部。
        _workflow.executor = _executor
        _workflow.file_store = _file_store
        _workflow.job_state_store = _job_state_store
        logger.info(
            "[SyncSkills] Re-injected interfaces after workflow rebuild: "
            f"executor={type(_executor).__name__}, "
            f"file_store={type(_file_store).__name__ if _file_store else 'None'}, "
            f"job_state_store={type(_job_state_store).__name__}"
        )

        # ============================================================
        # 2026.05.22 George : v1.9 — Re-inject skills wiring
        # ============================================================
        # Workflow rebuild 後 coding_agent.context_providers 跟
        # workflow.skills_factory 都會掉,要跟 startup 4d 保持一致:
        # - Static 模式 (_skills_factory is None):重建 static provider
        #   (因為 sync 後 SKILLS_DIR 內容變了) 並注入 coding_agent
        # - Dynamic 模式:re-attach factory。Factory 內部的
        #   _content_cache 還有舊內容但 TTL=1h 會自然失效,不主動清。
        # ============================================================
        _workflow.skills_factory = _skills_factory
        if _skills_factory is None:
            new_static = _build_static_skills_provider()
            if new_static is not None:
                try:
                    existing = list(
                        _workflow.coding_agent.context_providers or []
                    )
                    non_skill = [
                        p for p in existing
                        if not isinstance(p, _StaticSkillsProvider)
                    ]
                    _workflow.coding_agent.context_providers = (
                        non_skill + [new_static]
                    )
                    logger.info(
                        "[Phase v1.9] Re-injected static SkillsProvider "
                        "after sync (SKILLS_DIR refreshed)"
                    )
                except Exception as e:
                    logger.error(
                        "[Phase v1.9] Failed to re-inject static provider: %s",
                        e, exc_info=True,
                    )
            else:
                logger.warning(
                    "[Phase v1.9] Static provider unavailable after sync "
                    "— coding_agent runs without skills"
                )
        else:
            logger.info(
                "[Phase v1.9] skills_factory re-injected "
                "(dynamic mode — factory cache will refresh on next turn via TTL)"
            )

        sync_text = f"Skills synced: {count} skill(s). Workflow rebuilt with new skills."
        # 2026.07.05 George : list_skills 改 async 後補 await。
        # sync_skills() 本身是管理操作、沒有呼叫者的 __user_token,
        # Dynamic 模式下會自然落入 list_skills 的 app/agent(admin bypass)
        # 分支,顯示完整目錄並帶警語——這裡是同步後驗證用途,可以接受。
        sync_text += "\n" + await list_skills()
    except Exception as e:
        logger.error(f"[SyncSkills] Failed: {e}", exc_info=True)
        sync_text = f"Skills sync failed: {str(e)}"
    return sync_text

async def clear_session(session_id: str) -> str:
    """清除指定 session 的 metadata。"""
    if _conv_store:
        await _conv_store.delete_metadata(session_id)
    logger.info(f"[Request] Cleared metadata for session={session_id}")
    return "✅ 對話歷史已清除,可以開始新的對話。"


# ============================================================================
# SKILL REVIEW: Human-in-the-Loop 審核
# 2026.03.30 George: v1.3
#
# Logic App Adaptive Card callback 透過 mcp_server.py REST endpoint 呼叫,
# 最終調用這裡的 approve/reject 函式。
#
# 核心設計:pending 存的是 knowledge JSON,approve 時才做 merge。
# 確保對「當下最新」的正式 SKILL.md 做增量合併,不會後蓋前。
# ============================================================================


# ============================================================================
# Mode B Dynamic Skills - Admin SQL Token Helper
# 2026.05.25 George
#
# Gatekeeper approve 後同步 skill metadata 到 SQL 時,需要繞過 RLS 寫
# user_skill_grants(一般 user OBO 會被 RLS 擋住寫別人的 grant)。
# 解法:用 ACA Managed Identity 拿 SQL admin token。
#
# 前置條件(SQL 端必須完成,見 mode_b_publish_deployment.sql):
#   1. CREATE USER [aca-app-name] FROM EXTERNAL PROVIDER
#   2. GRANT INSERT/UPDATE/SELECT ON skills + INSERT/SELECT ON user_skill_grants
#   3. RLS predicate function 加 SUSER_SNAME() = '[aca-app-name]' bypass
# ============================================================================

_admin_credential: Optional["AsyncDefaultAzureCredential"] = None  # type: ignore[name-defined]


async def _get_admin_sql_token() -> str:
    """
    用 ACA Managed Identity 取得 Azure SQL admin token。

    與讀路徑(OBO 三層交換)不同,寫路徑用 MI 直接取得 token,
    身分為 ACA Container App 本身(在 SQL 被認為是 admin)。

    Returns:
        Bearer token string,可傳給 gatekeeper_publish.sync_skill_to_sql()

    Raises:
        各種 azure.identity 例外(MI 未啟用、權限不足等)
    """
    global _admin_credential
    if _admin_credential is None:
        # 延遲 import,與檔內其他 Azure SDK 用法一致(避免汙染檔頭 imports)
        from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
        _admin_credential = AsyncDefaultAzureCredential()

    token_obj = await _admin_credential.get_token(
        "https://database.windows.net/.default"
    )
    return token_obj.token


async def approve_pending_skill(pending_id: str, reviewer: str = "unknown") -> dict:
    """
    2026.03.30 George: v1.3 HITL 新增

    審核通過:從 pending 讀回 knowledge JSON,對當下最新的正式 skill 做 merge 寫入。

    流程:
    1. 從 Blob skills-pending/{pending_id}/metadata.json 讀回 knowledge + 決策 context
    2. 呼叫 write_knowledge_skill(knowledge, existing_skill_dir)
       → 對「當下最新」的正式 SKILL.md 做 merge + dedup
    3. 上傳寫入結果到 Blob 正式路徑
    4. sync_skills(): Blob → 本地 + rebuild workflow
    5. 刪除 Blob skills-pending/{pending_id}/
    6. 記 log
    """
    # ── ① 讀 pending metadata ──
    pending = await blob_read_pending_metadata(pending_id)
    if not pending:
        logger.error(f"[SkillReview] Pending not found: {pending_id}")
        return {"status": "error", "reason": f"Pending ID {pending_id} not found"}

    knowledge = pending.get("knowledge")
    if not knowledge:
        logger.error(f"[SkillReview] Pending metadata missing 'knowledge': {pending_id}")
        return {"status": "error", "reason": "Pending metadata corrupted (missing knowledge)"}

    skill_name = knowledge.get("skill_name", "unknown")

    # ── ② 判斷 merge 目標 ──
    # 2026.08.24 George: merge 基準改以 Blob 為準。原本直接取本地 SKILLS_DIR 下的
    # 同名目錄，而本地只在啟動時同步一次——多 replica 或人工發布後沒重啟時，
    # 拿到的是舊版 SKILL.md，merge 出來的結果會把別人的更新蓋掉。
    # ETag 在這裡才取（而非寫 pending 時），因為人工審核之間可能隔很久，
    # 提早取只會製造假的 412。
    existing_skill_basename = pending.get("existing_skill_dir")
    existing_skill_dir = None
    existing_skill_etag = None
    if existing_skill_basename:
        from skill_gatekeeper import _resolve_and_refresh_skill_dir

        existing_skill_dir, existing_skill_etag = await _resolve_and_refresh_skill_dir(
            existing_skill_basename
        )
        if not existing_skill_dir:
            logger.warning(
                f"[SkillReview] Merge target '{existing_skill_basename}' "
                f"在 Blob 與本地皆不存在,將改為新建"
            )

    # ── ③ 對當下最新的正式 skill 做 merge(核心:避免後蓋前)──
    from skill_gatekeeper import write_knowledge_skill  # 延遲 import,避免循環依賴

    try:
        skill_dir = write_knowledge_skill(knowledge, existing_skill_dir=existing_skill_dir)
    except Exception as e:
        logger.error(f"[SkillReview] write_knowledge_skill failed: {e}")
        return {"status": "error", "reason": f"Write failed: {e}"}

    # 2026.08.14 George: 後續 Blob/SQL 一律以實際寫入的目錄名為準。merge 時落地的是
    # 既有 skill 目錄，沿用 knowledge["skill_name"] 會上傳不存在的目錄、並在 SQL
    # 多開一列孤兒 skill；另外目錄名已經 sanitize，能避開 schema 的名稱格式 CHECK。
    if skill_dir:
        skill_name = os.path.basename(skill_dir)

    # ── ④ 上傳到 Blob 正式路徑 ──
    # 2026.08.24 George: 帶 ETag 做 If-Match。上傳沒成功就整支中止 —— 保留 pending
    # 讓審核者重按一次即可:步驟 ② 會重新從 Blob 取基準，等於自動 rebase 到最新版，
    # 連本地被改髒的 SKILL.md 也會一併被覆蓋回正確內容。
    blob_ok = await blob_upload_skill(skill_name, skill_md_etag=existing_skill_etag)
    if not blob_ok:
        logger.error(
            f"[SkillReview] Blob upload 未完成，中止 approve(pending 保留供重試): "
            f"pending_id={pending_id}, skill={skill_name}"
        )
        return {
            "status": "conflict",
            "pending_id": pending_id,
            "skill_name": skill_name,
            "reason": "Blob 上傳未完成(SKILL.md 已被其他來源更新，或目錄不存在)，請重新 approve",
        }

    # ── ④.5 同步 metadata 到 SQL(Mode B Dynamic Skills 必要) ──
    # 2026.05.25 George : 配合 Mode B 讀路徑(SkillsProviderFactory)
    # - Refine 既有 skill: UPDATE skills.updated_at(觸發 cache invalidation)
    # - 新增 skill:        INSERT skills + INSERT user_skill_grants 給 admin
    # 用 ACA MI 拿 admin token,繞過 RLS 寫 grants
    try:
        from gatekeeper_publish import sync_skill_to_sql, PublishError

        admin_sql_token = await _get_admin_sql_token()

        sync_result = await sync_skill_to_sql(
            sql_token=admin_sql_token,
            skill_name=skill_name,
            # 2026.08.14 George: schema v2 拿掉 description，且 blob_path 改為
            # computed column —— 二者都不再由 Python 端提供。
            # 2026.08.14 George: schema v2.1 另加了 blob_prefix（也是 computed）
            # 供 Mode B 列舉整個 skill 資料夾；同樣不需要這邊傳。
        )
        logger.info(
            f"[SkillReview] SQL synced: skill={skill_name}, "
            f"created={sync_result.created}, granted_to={sync_result.granted_to}"
        )
    except PublishError as e:
        # SQL sync 失敗不 fail 整個 approve:
        # - Blob 已寫,本地已寫,static 模式 user 看得到
        # - 僅 Mode B (DYNAMIC_SKILLS_ENABLED=true) users 暫時看不到新版
        # - Ops 後續可手動補同步 SQL
        logger.error(f"[SkillReview] SQL sync failed (local + Blob OK): {e}")
    except Exception as e:
        logger.exception(f"[SkillReview] SQL sync unexpected error: {e}")

    # ── ④.6 主動失效 RLS 清單快取(Mode B) ──
    # 2026.07.20 George : #3 — publish 後不能只靠 SkillsProviderFactory 的短 TTL
    # 被動到期(demo 現場發布完立刻展示會踩到)。SQL grants/updated_at 一變,就
    # 主動清掉 RLS 清單快取,讓 Mode B 使用者下一個 turn 立刻看到新 skill;TTL
    # 只當保底防線。發佈時無法得知受影響 principal,故清全部。
    if _skills_factory is not None:
        try:
            _skills_factory.invalidate_rls_cache(reason="approve_pending_skill")
        except Exception as e:
            logger.warning(f"[SkillReview] RLS cache invalidate failed: {e}")

    # ── ⑤ sync_skills(): Blob → 本地 + rebuild workflow ──
    await sync_skills()

    # ── ⑥ 清理 pending ──
    try:
        await blob_delete_pending(pending_id)
    except Exception as e:
        logger.warning(f"[SkillReview] Pending cleanup failed: {e}")

    logger.info(
        f"[SkillReview] ✅ APPROVED: pending_id={pending_id}, "
        f"skill={skill_name}, action={pending.get('action_type')}, "
        f"reviewer={reviewer}"
    )

    return {
        "status": "approved",
        "pending_id": pending_id,
        "skill_name": skill_name,
        "action_type": pending.get("action_type"),
        "reviewer": reviewer,
    }


async def reject_pending_skill(pending_id: str, reviewer: str = "unknown") -> dict:
    """
    2026.03.30 George: v1.3 HITL 新增

    審核拒絕:刪除 pending,正式目錄不做任何變動。
    注意:不觸發 sync_skills(),因為正式目錄沒有任何變動。
    """
    # 讀 metadata(只為了 log,讀不到也不影響 reject 操作)
    pending = await blob_read_pending_metadata(pending_id)
    skill_name = "unknown"
    if pending:
        skill_name = pending.get("knowledge", {}).get("skill_name", "unknown")

    # 刪除 pending
    try:
        await blob_delete_pending(pending_id)
    except Exception as e:
        logger.warning(f"[SkillReview] Pending cleanup failed: {e}")

    logger.info(
        f"[SkillReview] ❌ REJECTED: pending_id={pending_id}, "
        f"skill={skill_name}, reviewer={reviewer}"
    )

    return {
        "status": "rejected",
        "pending_id": pending_id,
        "skill_name": skill_name,
        "reviewer": reviewer,
    }