# MCP 自動轉 capability skill：暫緩提案與實作交接

> **狀態：暫緩，未授權功能實作。**
> 本文件保存需求、已確認的設計與實作構想，不表示目前產品已具備下述功能。
> 未來啟動前必須重新核對 Generator、EAA runtime 與共同契約，更新計畫並取得確認。
> 本文件可獨立閱讀，不需要找回原始討論 session。

## 1. 目的與定位

使用者選擇 MCP server，由 Generator 探索工具、提議分組，經人工調整後自動產生 capability skill，不必自行撰寫 MCP 呼叫程式或從空白進行完整需求訪談。

現有 sample code 已能說明如何呼叫 MCP；此提案解決的是大量 server／工具的生成與維護規模，不是新增「能呼叫 MCP」這件事。少量且由工程師維護的整合仍可使用既有流程。

主要流程：

**選擇／註冊 server → discovery → 工具分組與確認 → 自動生成 → 人工審閱 → lint／neighbor／儲存。**

- 一次完整交付，不分 MVP；但需求尚待確認是否啟動。
- MCP capability 以工具 wrapper 為主，提供每組一個選填補充說明，不新增流程設計器。
- 跨能力情境編排交給 scenario；強制順序、交易或補償不由文字提示保證。
- 不新增 skill type：沿用 `metadata.skill_type: script`。

## 2. 查核基準與閱讀順序

| 項目 | 封存基準 |
|---|---|
| Generator repository | `georgeliang0821/foundry-skill-generator-v3` |
| Generator HEAD | `24333f2cf6d835b98067458b96d131b05c025fbc` |
| 本機 EAA checkout HEAD | `56627c1502885d97b8cb9011437e20faba1dd049` |
| EAA 本機 checkout 定位提示 | `C:\dev\code_tool_hosted-async-1.8.0`，非可攜依賴 |
| EAA 契約定位 | 討論指定 `docs/mcp_skill_contract.md`；先前實際讀取為 `doc/mcp_skill_contract.md`，需重新確認正式路徑 |

上述 SHA 是封存時取得的定位點，**不表示已對該 HEAD 重新完成契約或程式行為驗收**。先前讀過的契約仍為 Table／Key Vault 版；下述 SQL／baseline 等決策部分來自使用者轉述的 EAA 確認，啟動時必須對照 EAA 最新正本，不將本提案視為其實作證據。

先閱讀：

- [產品與使用者流程](../01-overview.md)
- [環境與身分設定](../02-setup.md)
- [架構與驗證資料流](../03-architecture.md)
- [Agent、形式與 prompt 機制](../04-agent-mechanism.md)
- [Reference 同步規則](../05-reference-snapshot-sync.md)
- [測試分層](../../tests/README.md)

## 3. 已確認範圍與責任

### 3.1 支援範圍

- 通用遠端 Streamable HTTP MCP server，不依賴 APIM／特定 gateway；包含此 transport 的 JSON／SSE 回應。
- 不含舊式 HTTP+SSE、stdio、mTLS、其他 IdP 的互動授權。
- 讀寫 tool 都支援；annotations 不做特別風險標記、權限判斷或執行前確認。
- 人工審核後才儲存，只防誤用，不防惡意 script；不增加 broker／網路隔離。
- 同一工具可出現在多個 skill，不建工具歸屬表；重疊交由 neighbor 分析提示。
- Gatekeeper 維持原行為，不新增 `refine_policy` 或 MCP 排除。
- `MCP_SKILLS_ENABLED` 預設關閉；關閉時不啟動新增 registry／token／連線工作，helper 明確拒絕。

### 3.2 Generator／EAA 分工

| Generator | EAA runtime |
|---|---|
| 註冊、discovery、分組、生成、保護、MODIFY 重新同步 | registry 唯讀、憑證準備、平台 helper、MCP 執行、輸出驗證與錯誤交付 |
| 保證 baseline 與 script `ALLOWED` 完全一致 | 不解析 `ALLOWED`，限制當前 skill 已宣告的 server |
| 人工審閱與現有儲存流程 | 不做 `tools/list`／執行前 schema 預檢 |
| 不接收 drift 通知、不建 drift 通知列表 | 偵測問題時停止後續執行，前端告知使用者聯絡 `created_by` |

不做排程漂移偵測、Graph scope 推斷、獨立 schema 歷史庫或 registry schema snapshot。保留既有完整套件備份，其中可包含當時 schema；重新同步只比較目前套件。

## 4. Server registry、身分與 secret

### 4.1 Azure SQL

沿用既有 SQL 連線設定，不使用 Azure Table 或新增 Table 環境變數。EAA 維護 DDL、RLS、契約正本與部署遷移；Generator 鏡像測試 schema，不自行建立正式表。

- `mcp_servers`：server ID、顯示名稱、URL、transport、enabled、credentials 規則、timeout、回應大小上限、created_by、updated_at、version。
- `mcp_server_secrets`：以 `(server_id, secret_id)` 區分多把 key。
- API-key `secret_ref`：`sql:{server_id}:{secret_id}`；識別碼可用字元／編碼由共同契約定義。
- 預留 `kv:{uri}` 擴充空間，本次不使用 Key Vault。
- 所有登入者可註冊、可讀取及選用任何啟用 server；只有建立者可更新、停用、刪除。
- 使用者 CRUD／secret 寫入走 Generator OBO＋SQL RLS，不以 SP 替使用者寫入。
- Generator 既有 SP 僅在伺服器端為 discovery／重新同步讀 secret；runtime MI 唯讀。RLS 明確允許兩個平台身分讀取，不只授予 SELECT。
- 使用者 secret 僅建立者可讀寫，API 仍不回傳原值；平台讀取例外不開放給使用者。
- API key 表單寫入專用，不進 log、LLM、session、瀏覽器持久儲存或錯誤回應；僅回傳「已設定」狀態。
- Registry／secret 更新需 SQL 交易一致性及版本條件寫入；衝突明確回報。
- 每筆必填 `timeout_seconds`、`max_response_bytes`；表單預填 `30`、`5242880`（5 MiB），缺值不可靜默補上。

### 4.2 憑證規則

支援多條 `header`／`source`／`scope` 或 `secret_ref`／`prefix` 規則；source 為 `none`、`api_key`、`oauth_passthrough`、`managed_identity`。

- 完全匿名用空規則陣列；全部必要憑證成功才連線。
- Header 名稱不分大小寫檢查重複；禁止 CR/LF、覆寫 transport 保留 header，prefix 不自動補空白。
- URL 不得含 userinfo／明文憑證／fragment；正式環境 HTTPS，不將憑證轉送到其他目的地。
- `oauth_passthrough` 是 Entra OBO，不是原樣轉送 token。
- Generator 用自己的 OBO client，與 EAA 分別取得 consent；需要時產生兩條 tenant-specific admin consent 連結。
- 已確認 `AADSTS65001` 等缺 consent 才呈現 consent 提示，不將所有認證失敗誤判為缺 consent。
- MI 使用相應平台身分，不以開發者登入或其他 credential 靜默替代；Generator MI 成功不代表 runtime MI 已有權限。

## 5. 註冊與 discovery

註冊依序嘗試：

1. 匿名 MCP 連線；401 解析 RFC 9728 `WWW-Authenticate` 的 `resource_metadata`，必要時嘗試對應的 well-known 位置，包含 URL path 規則。
2. 讀取 `resource`、`authorization_servers`、`scopes_supported`；驗證合法 metadata／resource／同租戶 Entra issuer，符合時採 OBO 候選，不自動請求所有 scopes。
3. 以 URL 對應的 `/.default` 作受控 OBO 候選，不保證該 URL 就是 Entra resource，不透過 Graph 推斷。
4. 失敗後提供範本或通用手動注入表單，不隱藏失敗原因。
5. 帶完整憑證測試，通過才交易保存 registry／secret。

範本僅預填通用規則：Azure AI Search／Foundry IQ、Functions `x-functions-key`、APIM subscription key／Entra、一般 Bearer。非 gateway 專用實作分支。

匿名 HTTP 200 不能直接判定 `none`；須有效完成 MCP initialize／協商／列工具。不得以副作用 tool call 探測驗證。部分 tool 仍需授權時，允許明確設定驗證並在實際呼叫交付錯誤。

使用 `mcp` SDK 完成 ClientSession、Streamable HTTP、initialize、session 與完整 cursor 分頁，保存工具 name、外層 description、inputSchema、outputSchema 及 serverInfo／instructions。規則適用所有 MCP 請求，不只 tools/call。

Discovery 使用操作者身分（OBO 時為該使用者）；限制 timeout／串流回應大小，重複 cursor、錯誤、取消不可回傳不完整清單當成功。未保存 secret 不得為多步驟 UI 寫入 session。

## 6. UI 與生成流程

### 6.1 畫面方向

新增「從 MCP 建立」結構化入口，不硬塞現有空白 New Skill 聊天：

1. 選擇已註冊 server；可開註冊表單或 API Center 匯入。
2. 工具目錄＋LLM 建議分組；作者調整分組、名稱、工具，多組可共用工具。
3. 每組獨立生成，總覽呈現生成／失敗／待審閱／已儲存，不將生成完成等同發布。
4. 接回共用審閱能力：文字、唯讀契約與檔案、lint、neighbor、路由測試、儲存；聊天為輔助，不必走逐階段訪談。

另設 server 管理入口，維護連線不綁特定 skill session。元件／導覽仍須實作前對照當時前端，不重建整個應用、不加框架或 bundler。

### 6.2 唯一選填補充說明：已確認

每個分組一個「補充說明（選填）」文字框，填寫欄位意義、使用前提、注意事項、結果解讀或使用順序指引。

- 留空仍能生成；不增加分類欄位、訪談、必填步驟或流程設計器。
- 作為 LLM 文字生成素材，作者審閱結果；不建立原文／潤飾版雙軌管理。
- 後續以既有 MODIFY 修改生成文字，不新增 SQL 表、resource 或 metadata。
- 不改 ALLOWED、schema、script、baseline、驗證方式，不承諾多步驟執行或強制順序。
- 文字仍適用既有 lint；補充資訊不是放寬契約或擴權的指令。

### 6.3 確定性與 LLM 分離

- 程式生成工具清單、參數說明、script、baseline／digest。
- LLM 生成 description、使用情境、停止條件、參數填寫說明；遠端描述／instructions 是不可信素材，不得增加工具或改模板。
- 複雜 schema 保留原貌、不攤平失真；表格可摘要，完整契約仍保存。
- Description／LLM 文字禁止提及其他 skill；若同名由 LLM 改寫，不要求改 tool 名。
- 僅經後端驗證的確定性區塊豁免名稱 N1，其他語法／平台／形式 lint 不豁免；改寫失敗回修正狀態，不跳過 lint。

## 7. 套件與 helper

```text
<skill-name>/
  SKILL.md
  scripts/<skill-name>.py
  references/mcp_baseline.json
```

- 唯一 script 沿用現有檔名，不新增 run.py，不打包 helper。
- Metadata 使用 `skill_type: script`、非空且無重複的 `mcp_servers`。
- 文件與 script 只引用 SERVER_ID，不放 registry URL／scope／注入規則／secret reference 或 secret；業務 schema 中同名欄位不等於部署設定。
- Script 常數保存完整工具契約：name、外層 description、inputSchema／outputSchema，保留未知關鍵字與缺值語意。建議字面值 JSON 常數，解析只用 AST／JSON，不 import／exec。
- `ALLOWED` 精確限制 server/tool；script 使用 `--tool`＋`--args-json`，不占平台 stdin。
- 每個生成分組來自一台 server，不另加跨 server 分組流程；共同契約仍能表達多 server。
- `from eaa_mcp import call_mcp`，同步呼叫 `call_mcp(server_id, tool_name, args)`；不接受 skill 自訂 URL／header／scope。
- 結果保留多 content blocks、structured content 與錯誤語意；精確以 EAA helper 契約為準，不把文字一律當 JSON。
- 失敗非零結束，不自動重試 tool call，特別是遠端寫入結果不明時。
- 禁止 MCP 呼叫傳播路徑上的 `except Exception`、bare except、`except BaseException` 或等價過寬捕捉。EAA 控制標記、BaseException 衍生例外與 drift 後 helper 永久失敗由 runtime 負責，確切型別啟動時核對。

## 8. Baseline 共同契約

### 8.1 Frontmatter 僅三欄

```yaml
metadata:
  skill_type: script
  mcp_servers:
    - search-mcp
  mcp_baseline:
    file: references/mcp_baseline.json
    algorithm: mcp-tool-schema-jcs-sha256-v1
    digest: "<64-character lowercase hex>"
```

`mcp_baseline` 只能有 file／algorithm／digest，不新增任何名稱含 version 的 key。理由為 EAA Gatekeeper 現有整份文件 version regex 可能誤改。沒有 description hash、預檢開關或另一份輸出 schema 檔。

### 8.2 Resource 頂層為 array

```json
[
  {
    "server_id": "search-mcp",
    "tool_name": "search",
    "structure_hash": "<64-character lowercase hex>",
    "output_schema": null
  }
]
```

每個 ALLOWED 的 server/tool 恰好一筆，條目僅四欄，區分大小寫，不得重複、缺漏或多出。EAA 不解析 ALLOWED，完全依賴 Generator 保證一致。即使全部沒有 outputSchema，也不能省略條目或檔案。

### 8.3 結構 hash：JCS

`mcp-tool-schema-jcs-sha256-v1` 的輸入固定：

```json
{"name": "<tool name>", "inputSchema": {}, "outputSchema": null}
```

- 實際使用完整 live inputSchema／outputSchema；outputSchema 未提供時固定 null，與 `{}` 不同。
- 不納入 server_id、外層 title／description／annotations、演算法版本；schema 內 description 等仍納入。
- RFC 8785 JCS canonical UTF-8 bytes → SHA-256 → 小寫 hex。
- 不以一般 `sort_keys=True` 替代 JCS；需 UTF-16 property 排序、ECMAScript 數字語意，不正規化字串、不改陣列順序。不合法或無法無損表示的輸入明確拒絕。
- 與 EAA 共用固定向量：key 順序、UTF-16／Unicode、數字／負零／指數、缺 outputSchema、null／{}、陣列順序、非法輸入。向量含預期 canonical bytes／hash。

### 8.4 檔案 digest：不是 JCS

- Baseline 檔本身用固定 JSON serializer 與穩定外層順序輸出，UTF-8 無 BOM、LF；不排序 schema 內部陣列。
- Digest 對**最後實際上傳的原始 bytes**做 SHA-256 小寫 hex，不正規化。
- 格式化或 CRLF 轉換後必須重算；應在最後產生 bytes，計算並直接上傳同一份，不再經 Windows text I/O 轉換。
- Runtime 對下載的原始 bytes 驗證，不能 parse／重序列化後才 hash。
- Digest 是一致性錨點，不是簽章；未知演算法拒絕，不當成無 drift。

### 8.5 本地輸出驗證

- 支援 Draft 2020-12；`$ref` 僅同一 schema 內 JSON Pointer，例如 `#/$defs/X`。
- 不支援外部／遠端 `$ref`、`$dynamicRef`、anchor reference，不自動抓取遠端。
- 轉換／同步檢查每個 outputSchema，不支援時 creator 選擇保留工具但略過本地驗證，或排除工具；不靜默降級。
- 略過時 baseline `output_schema: null`，但 script 保留完整 live schema，structure_hash 仍對完整 live outputSchema 計算。
- 沒提供 outputSchema 時存 null；已提供 `{}` 必須保留 `{}`，除非 creator 明確選擇略過。
- 略過驗證選擇記錄於受保護 script 契約狀態，不擴充 baseline／frontmatter 欄位。
- 無 outputSchema 的必要業務輸出欄位檢查待定，不猜測欄位、不先承諾強制 lint。

## 9. 保護與 MODIFY 重新同步

- 首次由作者審閱建立基準；既有 skill 只有 created_by 能修改，baseline 僅在 MODIFY 專用同步確認後更新。
- 一般 patch、直接編輯、undo、material patch、neighbor 儲存、最終 save 都不能改確定性表格／script／schema／baseline／digest。
- 不能靠刪 mcp metadata、形式轉換、資源操作或替換 script 解除保護；rename 僅改一致的名稱／路徑，不偷改工具契約。
- 後端依模板／script 契約交叉驗證，不只信任可修改的區塊 marker；缺失或不一致明確拒絕。

同步流程：

1. Creator 取得目前套件，安全解析 script 常數、ALLOWED、baseline／digest。
2. 以 creator 身分完整列工具；OBO 用 creator token，API key 用既定 SP secret resolver。
3. 顯示完整 diff：結構變更、description-only、新增、未列出工具；結構變更需審閱但不一律稱為破壞性。
4. 未列出工具標示「可能已移除，也可能是 creator 身分無權限」，不可斷言已刪除。新工具預設不選。
5. 確認工具集合、outputSchema 支援／略過選擇，檢查 skill／registry 未被並行修改。
6. 隔離草稿一致更新 script、表格、baseline、metadata digest；取消、discovery 失敗或 stale preview 不改原草稿／套件。
7. 不自動發布；提示審閱受影響文字，保留既有文字與 Gatekeeper Addendum，再走原保存流程。

Description 直接對 script 保存值 diff，不做 hash；description-only 若未改 baseline bytes，digest 不必改。Grant 同步維持選配，具體入口待定；grant 不自動接受 baseline，確認更新仍走 MODIFY。

不做 runtime 預檢或排程；相容的 schema／語意異動可能不被發現。Runtime 輸出驗證失敗時停止後續執行，但遠端寫入可能已完成，須先確認狀態，不自動重試。

## 10. 儲存順序與已接受風險

沿用 author ACL、雙層 lint、neighbor、版號、Blob／SQL／grant，不另加發布核准系統。EAA lint 無判定仍 fail closed。

本功能採**循序多檔發布，不做原子發布**：

1. 先寫 script 與 baseline resource。
2. 最後寫 SKILL.md，內含對已上傳 baseline bytes 的 digest。
3. 最後才更新 `dbo.skills.updated_at`，作為發布完成訊號。

任一步驟失敗即中止，不提前標完成、不回成功；向 creator 明確回報可能部分寫入，要求重新發布修復，不承諾自動 rollback。

**待 EAA 驗證：**skill cache 是否確實以 updated_at 作版本／失效依據，以及失敗後如何讀取／拒絕不一致套件。

Baseline 與 digest 不一致時 runtime 必須 fail closed。但 digest 不保護 script-only 更新，已接受其與一般 skill 發布相同的風險。舊快取、baseline 未變或特定失敗時點可能仍有相符 digest，因此不能宣稱所有部分失敗均拒絕、所有混版都被攔截或整包原子可見。

## 11. API Center 匯入

從配置的 API Center MCP registry endpoint 分頁列出候選 server，勾選後逐筆進相同註冊流程。

- Catalog 不是即時 MCP 契約或呼叫授權，不直接信任其憑證。
- 重複 server 提示使用既有紀錄，不覆寫他人 server。
- 顯示逐筆進度、成功／失敗／重試，重試不重建已完成項目。
- 實際 endpoint、API version、授權及去敏樣本仍待提供；catalog scope 與下游 scope 分開。

## 12. 實作落點與依賴

以下為查核時的既有函式與計畫新增名稱，未來須重新定位，不能將名稱視為已存在實作。

| 工作 | 沿用／修改 | 計畫新增與注意事項 |
|---|---|---|
| SQL／auth | [db.py](../../backend/db.py) 的 `run()`／`get_cursor()`；[main.py](../../backend/main.py) 的 `get_auth_record()`／`get_delegated_token()` | `mcp_registry.py`、`mcp_auth.py`；獨立 delegated connection，不能混用 SP thread-local pool |
| 註冊／discovery | 現有 FastAPI／身分驗證模式；既有單次 lint JSON-RPC 不當通用 client | `mcp_routes.py`、`mcp_discovery.py`；使用 MCP SDK、完整分頁與限制 |
| 分組／生成 | [models.py](../../backend/models.py) 的 Session、[agent.py](../../backend/agent.py)、[state_machine.py](../../backend/state_machine.py) 的 `build_system_prompt()`／`evaluate_skill_form()`／`lock_skill_form()` | typed MCP 狀態與選填補充，不新增 Stage，不假造使用者 code 素材 |
| Prompt | [prompts](../../prompts) 分層資料與註冊 map | MCP addendum，英文提示；不能只新增未註冊檔案 |
| 組裝 | [blob_store.py](../../backend/blob_store.py) 的 `script_relpath_of()`／`safe_skill_name()` | `mcp_skill.py`：compose／parse／integrity／render／diff；AST 不執行、最終 bytes digest |
| Script resource | [skill_assets.py](../../backend/skill_assets.py) 的 `assets_blocked_reason()`／`check_asset_set()`、形式判定、inventory／備份／復原 | 僅 MCP baseline 的受保護例外，需 EAA lint／載入配合 |
| 修改保護 | [main.py](../../backend/main.py) 的 `apply_tool_effect()`、`set_current_content()`、patch／undo／material／neighbor 寫入、`_save_skill_dual_write()` | 所有入口用共用完整性驗證，不能只在 UI 鎖定 |
| 形式轉換 | [form_conversion.py](../../backend/form_conversion.py) 的 `convert_session_form()` | 拒絕一般 MCP script→inline 繞過 |
| Lint | [skill_lint.py](../../backend/skill_lint.py) 的 `check_named_skill_references()`／`lint_skill()` | N1 精確遮罩確定性區塊；新增 MCP 過寬 except AST 檢查，其餘規則保留 |
| 儲存／neighbor | [main.py](../../backend/main.py) 的 `save_skill_dual_write()`／`_save_skill_dual_write()`、`_load_peer_skill_boundaries()`／`neighbor_edit_save()` | 擴充 MCP 發布順序與失敗訊息，外部 lint 送真實完整套件，不遮罩後冒充產物 |
| API Center | 共用註冊流程 | `mcp_api_center.py`，實際介面先核對 |
| 前端 | [api.js](../../frontend/js/api.js) 唯一 fetch 入口、[main.js](../../frontend/js/main.js)、[index.html](../../frontend/index.html)、[style.css](../../frontend/css/style.css) tokens | 可新增 MCP UI ES module，不加 bundler；secret request 不 console log |

依賴順序：

1. EAA 契約／SQL DDL／RLS／helper／baseline 契約核對。
2. Registry auth → discovery／註冊；確定性組裝可在契約確認後獨立進行。
3. Discovery＋組裝 → 分組生成 → 修改保護／重新同步。
4. Discovery → API Center 匯入。
5. 生成／同步／匯入 → UI 整合 → 聯合驗收與文件。

這是工作相依，不是分期交付。不導入本提案以外的工具治理、工作流引擎或通用資源放寬。

## 13. 驗證與文件

- Pytest unit：RFC 9728 challenge／path well-known、issuer／tenant／resource 錯誤、OBO／consent、範本／手動規則、多 secret／header、SDK JSON／SSE／分頁、取消／超限、schema 無損往返、AST 安全解析。
- Hash／baseline：跨端 JCS 向量、四欄 array／三欄 metadata、ALLOWED 一致、未知演算法、最終 bytes digest、BOM／LF／CRLF、null／{}／opt-out、description-only 不變 digest。
- 輸出 schema：Draft 2020-12、本地 pointer、外部／anchor／dynamic ref 拒絕、creator 選擇及完整 live hash，不自動抓取外部資源。
- Pytest API：登入／creator ACL、OBO／SP 分工、secret 不洩漏、交易／版本衝突、所有修改繞過入口、N1 區塊、過寬 except、resync 失敗／取消／可見性不明、旗標關閉、選填補充留空／組間隔離。
- Playwright：deterministic fake 完成來源選擇、註冊、分組、選填補充、生成總覽、審閱、唯讀區、secret write-only、resync／取消與逐筆匯入；不碰真 Azure／OAuth。
- 真實 SQL：兩位使用者、Generator SP、runtime MI 的 RLS 與 DML；mock 不證明正式權限。
- 真實 EAA：none／API key／OBO／MI／多 header，生成套件載入、content blocks／structured content、讀寫 fixture、控制標記、例外、停止後續呼叫、不重播、created_by 指引。
- 發布故障注入：各檔與 updated_at 失敗、順序、cache／digest 拒絕、重新發布修復及 script-only 限制。
- Route-only 不執行 script，不能當 MCP 端對端成功證據；真寫入測試須指定可清理資料，不用 discovery 自動試寫。
- 沿用既有工具，先跑直接相關 selectors，再按影響擴大；`uv run` 一律 `--no-sync`。加 SDK 等依賴時更新 manifest／lock，依規則檢查 sync 影響，不順便加 formatter。
- 更新 setup／architecture／agent／使用指南與測試文件；繁中 prose、英文 code／prompt。
- `backend/` 不 import `reference/`，不手編 vendored 檔；EAA commit 就緒後透過 [sync_reference.py](../../scripts/sync_reference.py) 同步。

## 14. 風險與待確認事項

### 仍需外部輸入

- EAA 最新契約正本位置、版本、DDL／RLS、身分識別、secret_ref 編碼、交易與並行更新規則。
- Generator／runtime client ID、consent 與合法 redirect 設定；secret 不貼進文件或 issue。
- MI allowlist／平台憑證授權及精確 helper 例外、控制標記／輸出驗證契約。
- `updated_at` cache 行為、發布失敗的拒絕／恢復流程、created_by 前端聯絡資訊來源。
- EAA lint 對 MCP resource、N1 確定性區塊及 helper 程式形狀的相容性。
- API Center 的實際 endpoint／API version／授權樣本。
- 真實測試 server、scope、SQL 身分、讀寫 fixture／清理方式。

### 保留的未定案／限制

- Grant 同步為選配，具體入口與是否啟用待定；不自動接受 baseline。
- 無 outputSchema 的必要欄位檢查待定，不先生成推測規則。
- 超大 schema／工具數量需要明確限額與縮小分組提示，不截斷契約；精確 limit 對齊 runtime。
- 工具可見性依使用者不同；creator 的 discovery 不等於被授權者可用。
- Registry 建立者改 URL／驗證會影響其他人的 skill；版本與影響要揭露，刪除後明確失敗。
- 不保證所有 drift 被偵測、遠端寫入回滾、exactly-once 或原子發布。
- API Center 能列 server 不等於已取得下游權限。

## 15. 未來重新啟動程序

1. 先確認需求是否仍成立；本文件不等於開工授權。
2. 讀本文件與當前專案說明，定位兩端目前 commit／未提交變更。
3. 與 §2 基準比較，逐項列「仍有效／需調整／已失效／待確認」，不直接照舊函式名稱實作。
4. 取得 EAA 最新共同契約，重查 SQL、helper、lint、script resource、cache／發布語意。
5. 重查 Generator 的狀態機、形式、所有修改入口、保存與前端；不覆蓋期間新增的功能。
6. 更新實作計畫、範圍與驗收要求，取得明確確認後才實作。

可直接在任何新 session 貼上：

> 請閱讀 docs/proposals/mcp-capability-skills.md 及 EAA 最新共同契約。功能目前暫緩，先不要實作。請核對當前 Generator／EAA codebase 與文件記錄基準的差異，列出仍有效、需修改及待確認項目，再更新實作計畫供我確認。不要將封存提案視為已授權開發。

## 16. 封存交付狀態

- 本次僅建立提案文件，不修改功能、不 commit／push。
- 使用者審閱並手動 commit／push 後，再建立追蹤 issue，連到已發布文件。
- Issue 須標示暫緩與重新核對要求；不引用只能在原 session 開啟的檔案作唯一依據。
- 本文件是可攜的交接依據；若與後續 EAA 正本衝突，先共同確認再更新，不默默選邊實作。
