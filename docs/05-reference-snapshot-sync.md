# 05 - Reference snapshot 使用與同步指南

> 本文件回答兩個維護問題：本專案實際依賴 `reference/` 的哪些 function，以及 upstream 發生什麼變更時需要同步或重新檢查本專案。
>
> `reference/` 是外部 EAA runtime 的唯讀快照，不是本專案 production backend。禁止讓 `backend/` import 它；同步時應從同一個 upstream commit 整檔更新，不要把單一 function 手動摘進 `backend/`。

日常維護不需要人工逐檔計算 hash 或閱讀完整 diff。先執行：

```powershell
uv run --no-sync python scripts/sync_reference.py check
```

工具會分開回答兩件事：檔案是否 drift，以及已知 Generator 契約是否仍相容。只有出現 `[RED]` 時才需要依報告點名的 backend/test 檔案做人工判斷；`[YELLOW]` 代表有新快照可同步，但已知契約沒有改變。

---

## 1. 來源與目前狀態

| 項目 | 值 |
| --- | --- |
| Upstream repository | `https://github.com/agent-accelerators/enterprise-agent-accelerator.git` |
| 本機 upstream checkout | `C:\dev\code_tool_hosted-async-1.8.0` |
| Upstream branch | `main` |
| 本地 snapshot | `reference/` |
| 現況與 provenance 唯一來源 | `reference/manifest.json` |

最後核對 commit、日期、各 snapshot 版本與 SHA-256 由同步工具自動寫入 `reference/manifest.json`，不在本文件重複保存，以免每次同步後文件立即過期。使用下列命令確認 manifest 與本地 snapshot 的實際現況：

```powershell
uv run --no-sync python scripts/sync_reference.py check
```

版本相同不代表內容一定相同；`core_handler.py` 曾經在版本仍為 1.11 時改變契約。因此工具以固定 commit 的 Git blob 與 SHA-256 為準，不依賴 `VERSION` 或會移動的 `main`。`.gitattributes` 固定 `reference/*.py` 使用 LF，避免 Windows checkout 轉換換行後產生假 drift。

---

## 2. 本專案實際使用範圍

### 2.1 Production backend

目前 `backend/` **沒有 import 或執行**這三個 reference module。這是刻意的架構邊界，定義在 `backend/topology.py`：

- `backend/` 不得 import `reference/` runtime snapshot。
- Generator 自己的 frontmatter validator 採保守格式，不複製某一版 runtime parser。
- Upstream parser 改變時，先做相容性判斷，不應直接把 reference function 搬進 production code。

因此 upstream 的 job lifecycle、Teams 通知、conversation store 或 hosted-agent adapter 改變，通常只需更新 snapshot/provenance，**不代表 generator 要跟著改**。

### 2.2 本 repo 直接依賴的 function

| 本 repo caller | 使用的 reference symbol | 使用方式 | 目的 | Upstream 異動時的處理 |
| --- | --- | --- | --- | --- |
| `tests/unit/test_topology_differential.py::_reference_declares_children()` | `skills_provider_factory._declares_children(content)` | 讀取 `reference/skills_provider_factory.py`，用 AST 只抽出該 function 執行；不 import 整個 module。 | 驗證「Generator 接受的 scenario frontmatter，runtime 也判定為 parent」。 | function 被移除、改名、增加 closure/global 依賴、參數或回傳型別改變時，手動更新 differential test。若只是 runtime 接受更多格式，通常不需放寬 Generator。 |
| `reference/test_skill_topology.py::ScenarioFilterDirectionTests.test_only_skills_declaring_children_are_filtered()` | `skills_provider_factory._declares_children(content)` | 直接 import；屬 reference snapshot 自帶測試，不是 production test boundary。 | 確認只有自己宣告 `metadata.children` 的 parent 被排除於 internal-agent materialize。 | 與 factory 同 commit 整體同步；不要單獨保留舊測試。 |

單向相容條件是：

> Generator 判定合法的 scenario skill，upstream `_declares_children()` 必須回傳 `True`。

反方向不成立。Upstream parser 可能接受 tuple、較寬鬆 fence 等格式；Generator 可以繼續只產生所有已知 parser 都能讀取的保守格式。

---

## 3. Reference snapshot 內部呼叫矩陣

以下呼叫發生在外部 runtime snapshot 內。它們可用來判斷三個檔案是否必須成組同步，但不是 generator production runtime 的呼叫。

### 3.1 `core_handler.py` 使用 `skills_provider_factory.py`

| Caller function / 區域 | Callee | 執行條件 | 關鍵 contract | 何時要成組同步 |
| --- | --- | --- | --- | --- |
| Module import fallback | `PrefixTolerantSkillsProvider` | 載入 `core_handler` 時嘗試 import。 | 必須仍是 `SkillsProvider` 相容子類，且支援 `from_paths()`。 | class 改名、基底類別、constructor、`from_paths()` 或 resource resolution 行為改變。 |
| `_build_static_skills_provider()` | `PrefixTolerantSkillsProvider.from_paths()` | `DYNAMIC_SKILLS_ENABLED=false` 的 static Mode A。 | `resource_extensions`、`resource_directories` 與 `resource_directories` attribute 必須和 dynamic Mode B 一致。 | MAF `from_paths()` API、resource 掃描參數、prefix 容錯改變時，Mode A/Mode B 必須一起檢查。 |
| `startup()` | `SkillsProviderFactory(...)` | `DYNAMIC_SKILLS_ENABLED=true`。 | Constructor 接受 Blob client/container，並依 SQL env 建立 per-turn factory。失敗時不繞過 RBAC，只讓 agent 無 skills。 | Constructor、必要 env、SQL/Blob client ownership 或 failure mode 改變。 |
| `list_skills()` | `SkillsProviderFactory.list_allowed_skills_full(sql_token)` | Dynamic Mode B；user OBO 或 app/admin token 路徑。 | 回傳 `list[dict]`，至少有 `name`，並帶 `_is_internal`、`_files` 等底線欄位；`core_handler` 負責對外投影。 | 回傳欄位、internal skill 投影責任、RLS token 語意或錯誤行為改變。 |
| `fetch_skill()` | `SkillsProviderFactory.get_skill_content(sql_token, skill_name)` | Dynamic Mode B 的 progressive disclosure。 | 無權限與不存在都回 `None`，不得洩漏 skill 是否存在；internal child 仍可取。 | 權限邊界、回傳 sentinel、private/global 同名選擇或 internal 過濾改變。 |
| `approve_pending_skill()` | `SkillsProviderFactory.invalidate_rls_cache(reason=...)` | Gatekeeper approve 且 factory 已初始化。 | Publish 後清除所有 principal 的 RLS list cache；TTL 只是保底。 | Cache scope/key、publish invalidation 或 method signature 改變。 |

> `create_provider_scope()` 通常由 upstream `code_agent_hosted.py` 消費，不是在這份本地 `core_handler.py` 內直接呼叫。它仍是 factory 最重要的 runtime contract，詳見第 4.1 節。

### 3.2 `core_handler.py` 使用 `skill_gatekeeper.py`

| Caller function | Callee | 執行條件 | 關鍵 contract | 何時要成組同步 |
| --- | --- | --- | --- | --- |
| `approve_pending_skill()` | `_resolve_and_refresh_skill_dir(existing_skill_basename)` | 人工審核通過，且 pending 指向既有 merge target。 | 從 Blob 取得最新基準並回傳 `(skill_dir, skill_md_etag)`；找不到時可改成 new skill。 | 回傳 tuple、Blob fallback、ETag 來源或錯誤處理改變。 |
| `approve_pending_skill()` | `write_knowledge_skill(knowledge, existing_skill_dir=...)` | 取得最新 merge 基準後。 | 回傳實際寫入的 skill directory；後續 Blob/SQL 必須用其 basename，不能假設等於 `knowledge["skill_name"]`。 | 參數、回傳路徑、merge/new 判定、版本遞增或 exception 行為改變。 |

這兩個 function 與 `blob_upload_skill(..., skill_md_etag=...)` 是同一個 optimistic-concurrency contract。只同步 gatekeeper 而不檢查 core approve flow，可能造成舊內容覆蓋或新建 skill 的錯誤 `If-Match`。

### 3.3 Upstream adapters 對三個 module 的入口

| Upstream caller | 使用入口 | Runtime 角色 | 需要檢查的相容面 |
| --- | --- | --- | --- |
| `main.py::agent_run()` / `_stream_with_keepalive()` | `core_handler.is_ready()`、`list_skills()`、`sync_skills()`、`clear_session()`、`run_workflow()` | Foundry hosted adapter。 | async/sync、參數、回傳 envelope、stream timeout。 |
| `mcp_server.py` tools / lifespan | `core_handler.run_workflow()`、`check_pending_tasks()`、`cancel_pending_task()`、`clear_session()`、`list_skills()`、`fetch_skill()`、`sync_skills()`、`startup()`、`shutdown_inflight_jobs()`、`shutdown()` | MCP/REST adapter 與 process lifecycle。 | Tool schema、`credentials`、`mode`、`scenario`、`sections`、job status 與 shutdown contract。 |
| `code_agent_hosted.py` | `skill_gatekeeper.trigger_gatekeeper_async()` | CodingAgent 完成後非同步萃取/寫回。 | `GatekeeperPayload` 對 ConversationState 的映射、`skills_referenced`、decision post-processing。 |
| `code_agent_hosted.py` | `SkillsProviderFactory.create_provider_scope()` | 每一 turn 建立 RLS-scoped provider。 | `sql_token/session_id/turn_id/scenario`、context-manager cleanup、empty-provider failure mode、tracking attributes。 |

若 upstream adapter 和三個 snapshot 在同一 commit 一起改 signature，三個 snapshot 應從該 commit 成組更新。只複製被呼叫的 function，會遺漏 dataclass、enum、cache 或 helper 的隱含 contract。

---

## 4. Function-level 同步觸發條件

### 4.1 `skills_provider_factory.py`

| Function / class | Generator 對應面 | 需要手動審查或更新的變更 |
| --- | --- | --- |
| `_declares_children(content)` | `backend.topology.declared_children()`、`tests/unit/test_topology_differential.py` | Parser fence/YAML 規則、`metadata.children` 所在位置、可接受型別、空值語意、回傳型別、改名或移除。這是目前唯一由本 repo 測試直接執行的 reference function。 |
| `_declared_child_names(content)`（upstream 1.14 新增） | `backend.topology.declared_children()`、scenario 產生/修改流程 | 若 runtime 不只判斷 parent，而開始消費 children 名單作白名單，需檢查 Generator 是否產出完整、正確、可解析的 child/dependency 名單。 |
| `_dedupe_private_over_global(metas)` | `backend.skills_repo.list_skills_for_user()`、private/global skill 規則 | 同名 skill precedence、`skill_key`、owner/public/grant 語意改變。Generator 的 SQL 查詢複刻 `v_my_skills` WHERE，不能只更新 snapshot。 |
| `SkillsProviderFactory._fetch_allowed_skills(sql_token)` | `backend.skills_repo.py`、`db/skill_rbac_schema*.sql` | `v_my_skills` SELECT 欄位、RLS predicate、token principal、`is_internal`、`blob_path/blob_prefix`、cache key 改變。Schema migration 必須先於 runtime code。 |
| `list_allowed_skills_full(sql_token)` | Generator skill catalog、frontmatter contract | 回傳 metadata、`_is_internal`、`_files`、description source of truth 或單筆錯誤降級改變。 |
| `get_skill_content(sql_token, skill_name)` | Scenario progressive disclosure、ACL | 不存在/無權限行為、internal child 可見性、private-over-global 選擇或完整 SKILL.md 格式改變。 |
| `_fetch_skill_content(meta)` | `backend.blob_store.py`、cache/version contract | Cache key 不再使用 `(skill_key, updated_at)`、Blob decode/path、ETag/version source 改變。特別檢查 private skill 是否可能撞 cache。 |
| `_list_skill_files(meta)` | `backend.blob_store.py`、skill folder contract | `blob_prefix`、檔案白名單、容量/數量上限、path traversal 或 folder layout 改變。 |
| `_materialize_skills(metas, target_dir, scenario=...)` | `metadata.children`、scenario dependency 建模 | Parent 過濾、child/dependency whitelist、materialize 路徑或錯誤策略改變。這會直接改變 runtime 真正看得到哪些 skills。 |
| `create_provider_scope(sql_token, session_id, turn_id, scenario=...)` | Upstream MCP `scenario` 與 Generator TEST 的 route/execute contract | Signature、scenario 傳遞、清理時機、空 provider、exception/fail-closed 行為、yield type 改變。 |
| `PrefixTolerantSkillsProvider._resolve_resource_name()` | Skill 內 resource path 寫法 | basename fallback、ambiguity、resource directory 或 MAF dispatch 改變。若改成 fail-closed，需 lint/驗證 skill 中每個 resource path。 |
| `_TrackingSkillsProvider._load_skill()` 及 tracking attributes | Gatekeeper `skills_referenced`、執行追蹤 | MAF `_load_skill` signature、成功判定、`loaded_skills`/`loaded_resources` shape 或 route-only script guard 改變。 |
| `invalidate_rls_cache()` | Publish 後立即可見性 | Cache 從全域改 per-principal、reason/signature 或 SQL publish handoff 改變。 |

#### 已同步的契約變更：1.13 → 1.14

Upstream 1.14 新增 scenario 白名單收斂：指定 scenario 後，只 materialize 該 scenario `metadata.children` 列出的 skills。因此 `children` 不再只能理解為「本 scenario 新建的 internal child」，而是：

> 本 scenario 執行時可能使用的完整 runtime dependency whitelist，包含 delegated child 與既有共用 skill。

這和目前附件中的 `leave-workflow` 有直接關係。若 runtime 真正需要 M365 calendar skill 在 backend 內執行，就必須列入 `metadata.children`；但該檔明確寫的是 calendar 由**宿主**執行、backend 只執行 `hr-leave-system`，所以目前只列 `hr-leave-system` 與其流程描述一致。判斷標準不是「正文提到某能力就全部列入」，而是「該 skill 是否要被 internal runtime materialize」。

實際 signature 差異如下：

| Symbol | 本地 snapshot | Upstream |
| --- | --- | --- |
| `_declared_child_names(content)` | 不存在 | 新增；把 scalar string 正規化成單一元素 list，供 scenario scope 使用。 |
| `_declares_children(content)` | 自行解析並只接受非空 list/tuple | 改為 `bool(_declared_child_names(content))`，因此 scalar string 也視為 parent。 |
| `_materialize_skills(...)` | `(metas, target_dir)` | `(metas, target_dir, scenario="")`，先套 scenario whitelist 再落地。 |
| `_resolve_scenario_scope(...)` | 不存在 | 新增；scenario 空白、查無或沒有 children 時 fail-open，否則回傳 children set。 |
| `create_provider_scope(...)` | `(sql_token, session_id, turn_id)` | `(sql_token, session_id, turn_id, scenario="")`，把 scenario 傳給 materialize。 |

### 4.2 `skill_gatekeeper.py`

| Function / type | Generator 對應面 | 需要手動審查或更新的變更 |
| --- | --- | --- |
| `GatekeeperPayload.from_conversation_state(...)` | Upstream ConversationState、skills tracking | 新增/移除 state 欄位、`skills_referenced` 或 execution history 語意改變。 |
| `GatekeeperDecision` | Upstream SQL/Blob post-processing | `action_taken`、`skill_name`、`skill_md_etag`、`pending_id` 等欄位、enum 或 nullable contract 改變。 |
| `_has_extractable_knowledge()` / `_make_gate_classification()` | 無 generator runtime 直接依賴 | 只影響 upstream 寫回判定；更新 snapshot 即可，除非輸出會反向修改本 repo 管理的 SKILL.md。 |
| `extract_knowledge_with_llm()` / `extract_knowledge_by_rules()` | Skill 內容格式與 lint | Knowledge JSON schema、known issues、workflow patterns 或 prompt trimming 改變時，檢查 Generator 是否仍能解析/修改其產物。 |
| `_resolve_and_refresh_skill_dir()` | `core_handler.approve_pending_skill()` | 回傳 shape、ETag、新建 fallback 或 Blob source-of-truth 改變時，必須與 core approve flow 同步。 |
| `_is_orchestration_skill()` | `metadata.children` 與 factory `_declares_children()` | Parent 判定規則改變時，同時比對 factory、Generator topology 與 differential test，避免 gatekeeper 寫入 parent。 |
| `check_duplicate()` / `check_relevance_for_merge()` | Skill identity/merge 行為 | 比對 key、private skill、score 或候選來源改變時，檢查是否可能合併到錯誤 skill。 |
| `write_knowledge_skill()` | `core_handler.approve_pending_skill()` | 參數、回傳目錄、實際 skill name、merge/new 路徑或 version 更新改變。 |
| `run_gatekeeper()` | `code_agent_hosted.py` post-processing | Decision shape、auto/manual 分支、orchestration skip、ETag 或 SQL handoff 改變。 |
| `_write_to_pending()` / `_notify_logic_app()` | HITL approve/reject workflow | Pending JSON schema、Blob prefix、callback payload 或 retry/failure semantics 改變。 |
| `trigger_gatekeeper_async()` | `code_agent_hosted.py` | Signature、fire-and-forget/error containment 或回傳 decision 改變。 |

### 4.3 `core_handler.py`

#### 已同步但未升版的契約變更：1.11 → 1.11

本地與 upstream 都宣告 1.11，但 SHA-256 不同，diff 約為 231 行新增、13 行刪除。已確認至少包含以下公開 contract 變化：

| Symbol | 本地 snapshot | Upstream |
| --- | --- | --- |
| `run_workflow(...)` | `(user_input, session_id=None, credentials=None)` | 增加 `mode="execute"` 與 `scenario=""`；`route_only` 會回顯 mode，scenario 每輪傳入 factory 且不持久化。 |
| `fetch_skill(...)` | `(skill_name, credentials=None)` | 增加 `sections=""`；可用逗號分隔 H2 節名做 partial fetch，找不到節名時明確回錯。 |
| `_echo_mode(...)` | 不存在 | 新增；讓非 `execute` 模式在 completed/running/rejected 等 response shape 都可被 caller 驗證。 |

所以判斷 `core_handler.py` 是否要同步時不能只看 `VERSION`；應以 hash/diff 以及 upstream `mcp_server.py` 的呼叫參數為準。

| Function | Generator 對應面 | 需要手動審查或更新的變更 |
| --- | --- | --- |
| `startup()` / `shutdown()` | 外部 runtime lifecycle；generator 無直接呼叫 | Factory DI、必要 env、resource ownership 改變時更新 snapshot；通常不需改 generator。 |
| `_build_static_skills_provider()` | Mode A/Mode B skill resource parity | Resource extension/directory、provider class 或 MAF `from_paths()` 變更時，與 factory dynamic path 一起同步。 |
| `run_workflow()` | Router runtime/MCP TEST contract、scenario routing | `request/session_id/credentials/mode/scenario`、回傳 status/envelope 或 timeout/detach 行為改變時，檢查 `backend.testing.py` 與 API 測試。 |
| `_project_boundary_result()` | Generator 對 runtime response 的解析 | Whitelist 欄位、status、session/job id、token/diagnostic projection 改變。 |
| `cancel_pending_task()` / `check_pending_tasks()` | 測試 runner 若輪詢 async job | Job status、polling、picked-up 或 terminal result shape 改變時才需檢查 generator；純 storage 實作變更不需。 |
| `_declared_children()` / `_project_scenario_skills()` | `backend.topology.py`、`backend.skills_index.py` | `metadata.children`、`is_internal` 或 host catalog projection 改變。注意此 helper 接受 scalar string，Generator 仍可維持更保守的 list-only 輸出。 |
| `_format_skills_catalog()` / `list_skills()` | Generator skill catalog與 scenario discovery | `children` 呈現、frontmatter 欄位、files tree、OBO/admin 路徑或 internal projection 改變。 |
| `fetch_skill()` | Scenario progressive disclosure | 新增 `sections`、回傳格式、ACL、internal child 或 static-mode semantics 改變。 |
| `sync_skills()` | 外部 static snapshot；generator Mode B 不呼叫 sync | Dynamic runtime 若仍為每次 SQL+Blob 解析，通常不需同步 generator；Mode A 部署才需審查。 |
| `approve_pending_skill()` | Gatekeeper ETag/SQL/cache contract | `_resolve_and_refresh_skill_dir()`、`write_knowledge_skill()`、Blob If-Match、SQL publish、cache invalidation 任一改變時，與 gatekeeper/factory 成組同步。 |
| `reject_pending_skill()` | HITL pending schema | Pending delete/audit/callback contract 改變。 |

---

## 5. 同步決策表

| Upstream 變更 | 更新 reference snapshot | 檢查或修改 Generator | 判斷方式 |
| --- | --- | --- | --- |
| 三個來源檔內容或版本改變 | 是，三檔以同一 commit 整檔更新並記錄 provenance。 | 視以下契約而定。 | 不手摘單一 function。 |
| `_declares_children`、`_declared_child_names`、scenario whitelist 改變 | 是 | **是** | 檢查 `backend/topology.py`、`backend/state_machine.py`、`backend/main.py`、scenario tests。 |
| `v_my_skills`、RLS、`skill_key`、owner/public/internal 改變 | 是 | **是** | 檢查 `backend/skills_repo.py`、`backend/skills_index.py`、DB migrations；SQL schema 先部署。 |
| Blob path/prefix、private folder、skill files 過濾改變 | 是 | **是** | 檢查 `backend/blob_store.py`、save/import 流程與 folder tests。 |
| `list_skills`、`fetch_skill`、`run_workflow` 對外 contract 改變 | 是 | **是** | 檢查 `backend/testing.py`、Router runtime/MCP payload/response parser 與 API tests。 |
| Gatekeeper 產出的 SKILL.md/frontmatter schema 改變 | 是 | 可能 | 若 Generator 會 import、modify 或 lint 該產物，就檢查 parser/state machine。 |
| Gatekeeper internal scoring/prompt 改變，但輸入輸出 contract 不變 | 是 | 通常否 | 只更新 snapshot 與版本紀錄。 |
| JobStore、heartbeat、Teams 通知、conversation persistence 改變 | 是 | 通常否 | Generator 未執行 reference runtime；除非外部 response contract 同時改變。 |
| 只有註解、log 或 internal refactor，function contract/行為不變 | 是 | 否 | 更新 snapshot/provenance，跑既有相容性測試。 |

---

## 6. 自動檢查與同步流程

### 6.1 首次準備

確認 Generator 與 upstream repo 都存在：

```powershell
Test-Path C:\dev\foundry-skill-generator-v3
Test-Path C:\dev\code_tool_hosted-async-1.8.0\.git
```

若 upstream repo 尚未下載：

```powershell
git clone https://github.com/agent-accelerators/enterprise-agent-accelerator.git C:\dev\code_tool_hosted-async-1.8.0
```

安裝 Generator 開發環境：

```powershell
Set-Location C:\dev\foundry-skill-generator-v3
uv sync
```

若 upstream 不在 manifest 預設的 `C:\dev\code_tool_hosted-async-1.8.0`，每次可傳 `--upstream <path>`，或在目前 PowerShell session 設定：

```powershell
$env:REFERENCE_UPSTREAM_REPO = "D:\repos\enterprise-agent-accelerator"
```

### 6.2 取得最新版並檢查

先取得 upstream 最新 Git objects，再固定本次要檢查的 commit：

```powershell
git -C C:\dev\code_tool_hosted-async-1.8.0 fetch origin
$sha = git -C C:\dev\code_tool_hosted-async-1.8.0 rev-parse origin/main
$sha
```

接著在 Generator repo 根目錄檢查該 commit：

```powershell
Set-Location C:\dev\foundry-skill-generator-v3
uv run --no-sync python scripts/sync_reference.py check --commit $sha
```

> **務必注意：** 若省略 `--commit $sha`，工具只會檢查 `reference/manifest.json` 目前固定的 commit，不會自動 fetch 或檢查最新 `origin/main`。無參數形式適合確認「本地 snapshot 是否仍符合 manifest」，不適合偵測 upstream 新版。

只確認本地 snapshot 與 manifest 是否一致時，才使用：

```powershell
uv run --no-sync python scripts/sync_reference.py check
```

結果分成三類：

- `[GREEN]`：snapshot、manifest hash 與已知契約皆一致，不需動作。
- `[YELLOW]`：upstream 檔案有 drift，但 manifest 內列出的已知契約仍相容；可執行同步，不需要先閱讀整份 diff。
- `[RED]`：已知 function signature 或必要資料欄位改變。報告會列出應檢查的 Generator 檔案；先處理契約，不會覆蓋 snapshot。
- `[ACTION]`：snapshot 與 signature 都沒問題，但 Generator 與 runtime 的實際接線不完整——已接受的參數沒送出、runtime 會回傳的欄位沒分類或沒讀。報告直接點名要修改的檔案與欄位。

Exit code 為 `0` 表示全綠、`1` 表示 drift、`2` 表示契約不相容、`3` 表示 Git／路徑／測試等執行錯誤、`4` 表示有未使用的 runtime 能力。需要機器可讀輸出時加 `--json`。

### 離線模式（CI 使用）

upstream runtime 屬於另一個 organization，CI 無法 clone，因此 CI 跑的是：

```powershell
uv run --no-sync python scripts/sync_reference.py check --offline
```

`--offline` 不查 upstream，改用**已 commit 的 snapshot** 當作契約來源，狀態顯示為 `[PINNED]`。它能抓到的是 Generator 這一側的退步——必要參數不再送出、`required` 欄位不再被讀、snapshot 被手動改過（sha256 與 manifest 不符）。它**抓不到** upstream 出新版，那必須由開發者在本機跑不帶 `--offline` 的 `check`。`sync` 一定需要 upstream checkout，加 `--offline` 會直接拒絕。

CI 定義在 `.github/workflows/reference-check.yml`：exit `1`、`2`、`3` 讓 build 失敗，exit `4` 只發 warning——因為「Generator 還沒採用某個 runtime 能力」是待辦事項，不是回歸。

報告中另有兩個區塊，用途是把「整份 diff」收斂成「我要改哪裡」：

- `[SURFACE]`（列在各檔案下方）：只比對**對外契約**——function signature、class 欄位、模組層級的字串集合，忽略 function body。一次 231 行的 upstream diff 通常只會留下一兩個符號。
- `[IMPACT]`：拿上述改變的 token 去掃描 `backend/` 與 `tests/`，列出實際引用位置 `檔案:行號`。若顯示「No Generator file references the changed contract」，代表該變更是 runtime 內部細節，本專案不需要跟進——這是正確答案，不是漏掉。

要細看單一符號的前後原始碼：

```powershell
uv run --no-sync python scripts/sync_reference.py check --commit $sha --explain <SYMBOL>
```

`--explain` 會印出該符號在本地 snapshot（`local`）與 upstream commit（`upstream`）的完整宣告；某一側不存在時顯示 `(absent)`。

### 6.3 同步已檢查的 commit

確認上一個步驟為 `[YELLOW]` 且沒有契約破壞後，使用同一個 `$sha` 執行：

```powershell
uv run --no-sync python scripts/sync_reference.py sync --commit $sha
```

此命令會：

1. 直接用 `git show <commit>:<path>` 從 manifest 固定的 commit 讀取來源，不受 upstream 工作目錄目前 checkout 或未提交修改影響。
2. 在寫入前驗證 manifest 中的已知 function signature 與必要欄位。
3. 從同一 commit 整檔覆蓋三個 `reference/` snapshot。
4. 自動更新 `reference/manifest.json` 的 commit、日期、版本與 SHA-256。
5. 自動執行 manifest 中列出的 focused compatibility tests。

要檢查或同步指定的歷史 commit：

```powershell
uv run --no-sync python scripts/sync_reference.py check --commit <SHA>
uv run --no-sync python scripts/sync_reference.py sync --commit <SHA>
```

本機 upstream checkout 預設取自 manifest。其他機器可傳 `--upstream <path>`，或設定 `REFERENCE_UPSTREAM_REPO`。只有在除錯測試環境時才使用 `--no-tests`；一般同步不得跳過驗證。

同步完成後再確認本地狀態與 manifest 一致：

```powershell
uv run --no-sync python scripts/sync_reference.py check
git diff --check
git status --short
```

### 6.4 最短日常操作

已完成首次準備後，每次只需：

```powershell
git -C C:\dev\code_tool_hosted-async-1.8.0 fetch origin
$sha = git -C C:\dev\code_tool_hosted-async-1.8.0 rev-parse origin/main
Set-Location C:\dev\foundry-skill-generator-v3
uv run --no-sync python scripts/sync_reference.py check --commit $sha
# 只有結果為 YELLOW、沒有 RED 時才執行下一行：
uv run --no-sync python scripts/sync_reference.py sync --commit $sha
```

`git fetch origin` **每次都要執行**，不是一次性步驟。只有 `git clone` 與 `uv sync` 屬於首次準備。省略 fetch 時，`rev-parse origin/main` 會回傳本機快取的舊 commit，整個檢查就會對著過期版本進行而且照樣顯示綠燈。

若結果是 `[GREEN]`，不需同步；若結果是 `[RED]`，先依報告點名的 symbol 與 review files 修改 Generator 契約或測試，不要直接覆蓋 snapshot。

### 6.5 契約測試的責任

目前自動檢查涵蓋：

- `core_handler.run_workflow()` 與 `fetch_skill()` 的參數契約。
- scenario child parser、materialize 與 provider scope 的 signature。
- `GatekeeperDecision` 中 Generator 關心的必要欄位。
- Generator 接受的 scenario frontmatter 必須仍被 runtime 視為 parent。
- `backend/` 不得 import `reference/`。
- **Wire coverage**：`manifest.json` 的 `wire` 區塊宣告每個 runtime 參數必須由哪個 Generator 檔案送出。`dict_key` 比對 payload 字典鍵，`text` 比對字串常數內是否出現該參數名。
- **Response coverage**：`manifest.json` 的 `response_wire` 區塊把 `RESPONSE_BOUNDARY_WHITELIST` 的每個欄位分成 `required`（Generator 必須讀）與 `ignored`（附理由，明確宣告本專案不用）。upstream 新增欄位時它會落在兩邊之外而被點名；`required` 卻沒被讀、或 `ignored` 的欄位已被 upstream 刪除，也同樣點名。
- **Contract surface**：自動抽取 signature、class 欄位與字串集合並做前後比對，不需要在 manifest 逐一宣告，因此 upstream 新增的未知符號也涵蓋得到。

wire coverage 檢查 Generator **送出**什麼，response coverage 檢查它**讀回**什麼。`ignored` 必須寫理由，否則那些永遠不會用到的欄位（`job_id`、`existing_job` 等）會變成長期噪音，`[ACTION]` 就失去意義。

signature 相符只證明 snapshot 仍可解析，**不證明 Generator 真的用到**該參數。wire coverage 就是用來補這個落差；upstream 新增參數時，它會直接指出應該修改的檔案。

同步後若測試失敗，工具會停止並保留已同步檔案供檢查。依錯誤點名的 symbol 與 review files 修正後，重新執行同一個 `sync` 即可。若 `_declares_children()` 新增直接 helper 依賴，應只擴充 differential test 抽取的最小 function closure，不要改成 import 整個 reference module。

如需單獨重跑 focused suite：

```powershell
uv run --no-sync python -m pytest tests/unit/test_reference_sync.py tests/unit/test_topology_differential.py
```
