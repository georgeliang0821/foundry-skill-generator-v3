# 02 - 啟動與設定指南

> 本文件說明如何在**本機**啟動整個專案，以及需要哪些外部服務、環境變數與連線設定。
> 專案在做什麼請見 [01-overview.md](01-overview.md)；程式碼架構請見 [03-architecture.md](03-architecture.md)。

---

## 1. 需要的外部服務

這是一個本機執行、但**依賴雲端服務**的應用。啟動前請先備妥：

| 服務 | 用途 | 必要 |
| --- | --- | --- |
| **Microsoft Foundry Project（Agent）** | AI 大腦：對話 orchestrator 與 PREPARE 階段的網路研究 | 是 |
| **Microsoft Entra ID App Registration** | 使用者登入（OAuth2 授權碼 + PKCE），核發 access token | 是 |
| **Azure SQL Database** | 儲存 Skill metadata（`dbo.skills`）與使用者授權（`dbo.user_skill_grants`） | 是 |
| **Azure Blob Storage** | 儲存 `SKILL.md` 全文 | 是 |
| **APIM 端點（Router / Sync）** | 路由測試（TEST）與儲存後同步 Skill 清單 | 選用（TEST 功能需要） |

---

## 2. 前置工具

| 工具 | 版本 | 說明 |
| --- | --- | --- |
| **Python** | 3.10 – 3.13 | 見 `pyproject.toml` 的 `requires-python` |
| **uv** | 最新 | 套件管理與執行（`uv run ...`）。若公司網路封鎖公開 PyPI，需另外指定內部套件來源，見 [6. 疑難排解](#6-疑難排解公司網路擋住公開-pypi) |
| **Azure CLI（az）** | 最新 | 本機執行時用 `az login` 提供 Blob 驗證身分；該登入帳號必須具備 Blob 資料權限。Foundry 與 SQL 使用 App Registration 服務主體 |
| **Node.js / npm** | 最新 LTS | 必要；執行 `npm ci` 安裝 Cytoscape、Markdown 與語法上色等前端執行期套件。未安裝時 Agent Graph 無法繪製 |

---

## 3. 設定 `.env`

複製範本後填入實際值：

```powershell
Copy-Item .env.example .env
```

啟動時 `backend/main.py` 會 `load_dotenv(override=True)`，**`.env` 會覆蓋現有的環境變數**。

### 3.1 環境變數完整清單

#### 雲端服務使用的身分

網頁登入者與後端存取雲端服務的身分彼此獨立。依目前實作，各服務實際使用的身分如下：

| 服務 | 本機執行 | 部署至 Azure | Credential 實作 |
| --- | --- | --- | --- |
| **Microsoft Foundry Project** | `.env` 中 `AZURE_TENANT_ID` + `AZURE_CLIENT_ID` + `AZURE_CLIENT_SECRET` 對應的服務主體 | 同一個服務主體；目前不會因部署至 Azure 而自動改用 Managed Identity | `DefaultAzureCredential()`；完整的 `AZURE_*` 三件套會由 `EnvironmentCredential` 優先採用 |
| **Azure SQL Database** | 同一個 `.env` 服務主體 | 同一個服務主體 | `Authentication=ActiveDirectoryServicePrincipal`，沒有 Managed Identity 或 `az login` fallback |
| **Azure Blob Storage** | 目前 `az login` 的使用者帳號 | 應用程式的 Managed Identity | `DefaultAzureCredential(exclude_environment_credential=True)`，刻意不採用上述服務主體 |

> 瀏覽器的 Microsoft 登入只用來識別目前網頁使用者、套用 Skill ACL，以及取得需要的 delegated/OBO token；該使用者 token **不會**被轉送給 Foundry、SQL 或 Blob。由於 Foundry 與 SQL 共用 `AZURE_*` 服務主體，該服務主體必須分別取得兩邊的權限。

#### Foundry（AI 大腦）

請注意幫此 agent 在 AI foundry 上加入 Web Search 的 tool 方便他進行網頁搜尋來研究

Foundry client 使用 `DefaultAzureCredential()`。本專案同時要求 SQL 的 `AZURE_TENANT_ID`、`AZURE_CLIENT_ID`、`AZURE_CLIENT_SECRET`，因此這三個值也會組成 Foundry 實際使用的 `EnvironmentCredential`；本機的 `az login` 帳號通常不會被選到。請將該 `AZURE_CLIENT_ID` 對應的服務主體加入目標 Foundry project，僅呼叫既有 agent 時至少授予 **Foundry Agent Consumer**；需要 project data actions 時授予 **Foundry User**。

| 變數 | 必要 | 說明 |
| --- | --- | --- |
| `FOUNDRY_PROJECT_ENDPOINT` | 是 | Foundry 專案端點，例如 `https://your-project.services.ai.azure.com` |
| `FOUNDRY_AGENT_NAME` | 是 | orchestrator agent 名稱，預設 `skill-generator-agent` |
| `FOUNDRY_AGENT_VERSION` | 是 | agent 版本，例如 `2` |

#### Microsoft Entra 登入（OAuth2 授權碼 + PKCE）

| 變數 | 必要 | 說明 |
| --- | --- | --- |
| `MICROSOFT_TENANT_ID` | 是 | Entra 租用戶 ID |
| `MICROSOFT_CLIENT_ID` | 是 | App Registration 的 client id |
| `MICROSOFT_CLIENT_SECRET` | 是 | App Registration 的 client secret |
| `MICROSOFT_OBO_SCOPE` | 是 | access token 要求的委派 scope，例如 `api://<app-id>/user_impersonation` |

#### Azure SQL（metadata 與權限）

| 變數 | 必要 | 說明 |
| --- | --- | --- |
| `AZURE_SQL_SERVER` | 是 | 例如 `your-server.database.windows.net` |
| `AZURE_SQL_DATABASE` | 是 | 資料庫名稱 |
| `AZURE_TENANT_ID` | 是 | Foundry 與 SQL 共用的服務主體（App Registration）驗證用；三個一組必填 |
| `AZURE_CLIENT_ID` | 是 | 同上；此 client id 對應的服務主體也必須取得 Foundry project/agent 權限 |
| `AZURE_CLIENT_SECRET` | 是 | 同上 |

#### Azure Blob（SKILL.md 儲存）

| 變數 | 必要 | 說明 |
| --- | --- | --- |
| `AZURE_STORAGE_ACCOUNT_URL` | 是 | 例如 `https://youraccount.blob.core.windows.net`；Blob 使用 `DefaultAzureCredential(exclude_environment_credential=True)`，不支援連線字串 |
| `AZURE_BLOB_CONTAINER` | 是 | 容器名稱，例如 `skills` |
| `AZURE_BLOB_PREFIX` | 否 | blob 前綴；**只能是 `skills`**（或留空，預設即為 `skills`）。`dbo.skills.blob_path` 是寫死 `skills/` 的計算欄位，填其他值會使 SQL 指向應用程式從未寫入的位置，因此服務會在啟動時直接拋錯 |

> Blob 驗證使用 `DefaultAzureCredential(exclude_environment_credential=True)`，會刻意忽略 `.env` 中供 Foundry 與 SQL 使用的 `AZURE_CLIENT_ID`、`AZURE_CLIENT_SECRET` 等服務主體設定。本機執行時，實際使用目前 `az login` 的個人帳號，因此必須將 **Storage Blob Data Contributor** 指派給該帳號；一般 **Contributor** 不包含 Blob data-plane 讀寫權限，仍會發生 403。部署至 Azure 時，則將 **Storage Blob Data Contributor** 指派給應用程式的 Managed Identity。

#### 本機開發 / 測試選項

以下都有合理預設，本機通常**不需要設定**（`.env` 已直接填入下列預設值）：

| 變數 | 預設值 | 說明 |
| --- | --- | --- |
| `SGV2_SESSION_DIR` | `./.sessions` | 撰寫中的 session JSON 檔案存放目錄 |
| `SGV2_SESSION_STORE` | `local`（檔案） | session 儲存方式：`local`＝存本機 JSON 檔；`blob`＝改存 Azure Blob（多實例共用才需要） |

#### APIM 路由測試（選用，TEST 功能需要）

這是 TEST 階段「路由測試」的端點。不設時，其他流程照常運作，只是無法跑路由測試。

| 變數 | 說明 |
| --- | --- |
| `SKILL_SELECTION_TEST_APIM_RUN_URL` | APIM Router 的 `/run` 端點；TEST 把正 / 負面範例送去，看 Router 是否路由到本 skill（讀取位置：`backend/testing.py`） |

```dotenv
# APIM router test
SKILL_SELECTION_TEST_APIM_RUN_URL=https://kurt-apim.azure-api.net/coding-tool-contoso-apis/run
```

> 已移除：舊版的 `SKILL_SYNC_APIM_URL`（儲存後同步 skill 清單給 Router）。本產生器現在只支援「動態載入」（Mode B）：runtime 每次請求都直接從 SQL + Blob 解析 skill，雙寫完成即生效，不需要任何 sync 步驟。若你的 runtime 仍採靜態快照（Mode A），需自行在外部呼叫 `sync_skills`。

> **Runtime 需求：`mode` 欄位**。路由測試在 REST body 頂層送 `mode`（與 `request`、`credentials` 平行），**一律送 `"route_only"`** —— capability 與 scenario 的每一層都是，沒有任何一層會執行技能。runtime **必須把收到的 `mode` 原樣回顯在回應頂層**；缺漏或不符會讓整批測試中止並回 **HTTP 502**（沒有降級開關 —— 未經確認的 mode 無法與「靜默升級成 execute」區分，而那會讓一次路由測試寫進真實資料）。若你的 runtime 版本早於此協定，需先升級才能跑路由測試。

#### MCP / ACA 環境變數查詢（選用）

設定後，PREPARE 階段會透過 MCP 讀取目標 Azure Container Apps（ACA）應用**目前已有的環境變數**與 **OBO scope 註冊表**，讓 Agent 在確認 skill 變數時能分辨「ACA 已有（reuse）」或「需新增（add）」。**四個都填才會啟用**；任一留空即停用（功能 no-op，不影響其他流程）。

| 變數 | 說明 |
| --- | --- |
| `MCP_ENDPOINT` | MCP 伺服器根 URL（`/mcp`），提供「列出 ACA 環境變數」的工具 |
| `ACA_APP_NAME` | 要查詢的 ACA 應用名稱 |
| `ACA_RESOURCE_GROUP` | 該 app 的資源群組 |
| `ACA_SUBSCRIPTION_ID` | 訂閱 ID |

### 3.2 Azure SQL 連線與驗證策略

後端使用 **`mssql-python`** 我們會分兩種不同角色 **(A) 用誰連資料庫**、**(B) 寫進去的是誰**。

#### A. 連線身分（誰打開 SQL 連線）

整個應用**共用一個固定身分**連線——**只支援服務主體（App Registration）**，由 `backend/db.py` 組裝連線字串：

| 連線身分 | 必要環境變數 | 驗證方式 |
| --- | --- | --- |
| **服務主體（App Registration）** | `AZURE_TENANT_ID` + `AZURE_CLIENT_ID` + `AZURE_CLIENT_SECRET`（三個都必填） | `ActiveDirectoryServicePrincipal` |

> 這個連線身分只決定「**能不能連、能不能讀寫資料表**」，它**不代表**當下在用網頁的人，也不會自動帶入登入者資訊。

#### B. 帶入身分（寫進資料表的是哪個人）

`dbo.user_skill_grants` 的 `user_upn` / `granted_by` 寫的是**目前網頁登入者的 email**，與上面的連線身分**無關**——email 是被當成「資料值」用 SQL 參數傳入的：

```text
網頁 Microsoft 登入 → access token 內的 email
  → cookie sgv2_auth → require_upn 取出 email 當 UPN
  → add_grant(skill, user_upn=你的email, granted_by=你的email)
  → 以 SQL 參數（?）INSERT 進 dbo.user_skill_grants
```

所以即使**所有人共用同一個 App Registration 連線**，每筆 grant 仍會記成各自登入的 email。**資料列級隔離（RLS）** 也是用這個 email 比對 `dbo.user_skill_grants`，決定你看得到哪些 skill（見 [03-architecture.md](03-architecture.md) 的登入章節）。

> 只有**私有 skill**（`is_public = 0`）才會寫這一列。公開 skill 的可見性直接由 `is_public = 1` 決定，不需要也不會產生 grant 列；把一支 skill 從公開改回私有時，才會補上作者自己的 grant，避免作者反而看不到自己的 skill。

> 可見性只由 `PATCH /api/skills/{name}/visibility` 寫入，儲存內容（接受 SKILL.md / patch / 改名）一律沿用現有值。切換一支 skill 的可見性時，它宣告的 **internal children**（不在 host catalog 的子 skill）會跟著一起切換；降回私有時 children 也會各自補上呼叫者的 grant。

#### 需要的資料表（Skill RBAC schema v2.2）

- `dbo.skills(skill_name, owner_upn NULL, is_public, is_internal, enabled, created_at, updated_at)`，另有 PERSISTED 計算欄位 `skill_key`（PK）、`blob_path`、`blob_prefix`
- `dbo.user_skill_grants(user_upn, skill_key FK CASCADE, granted_at, granted_by, expires_at)`，PK = (user_upn, skill_key)
- `dbo.v_my_skills`：可見性的官方定義（`enabled = 1 AND (未過期 grant OR is_public = 1)`）

建置腳本見 `db/skill_rbac_schema v2.sql` 及其後續 migration。**計算欄位不可寫入**，應用程式只寫 `skill_name` / `owner_upn` / `is_public` / `enabled`。

### 3.3 Entra App Registration 設定

登入採 **OAuth2 授權碼流程 + PKCE**。設定步驟：

1. 註冊一個 App Registration，取得 `client_id` / `client_secret`。
2. 新增 **Redirect URI**：`http://localhost:6274/api/auth/callback`（本機）。
3. 暴露一個委派 scope（delegated permission），例如 `api://<app-id>/user_impersonation`。

把上面取得的資訊填入 `.env` 對應變數：

| 環境變數 | 對應 App Registration 的 |
| --- | --- |
| `MICROSOFT_TENANT_ID` | 租用戶（Directory）ID |
| `MICROSOFT_CLIENT_ID` | Application (client) ID |
| `MICROSOFT_CLIENT_SECRET` | client secret 的值 |
| `MICROSOFT_OBO_SCOPE` | 暴露的委派 scope，例如 `api://<app-id>/user_impersonation` |

> 登入時實際請求的 scope 是 `openid profile offline_access {MICROSOFT_OBO_SCOPE}`；authority / authorize / token 端點由 `MICROSOFT_TENANT_ID` 自動推導，**不需另外設定**。
> 登入背後的流程（授權碼換 token、如何拿到 email、token 存哪裡）請見 [03-architecture.md](03-architecture.md) 的「6. 登入與 Token 流程」。

---

## 4. 指派身分權限

Foundry 與 SQL 共用 `.env` 的 App Registration 服務主體；Blob 會排除該服務主體，本機使用 `az login` 帳號，部署至 Azure 時使用 Managed Identity。這些權限彼此獨立，請分別授權：

- **Foundry Project / Agent**：將 `AZURE_CLIENT_ID` 對應的服務主體加入目標 Foundry project。只需呼叫既有 agent endpoint 時授予 **Foundry Agent Consumer**；若還需要 project data actions，授予 **Foundry User**。一般 Azure **Owner**、**Contributor** 或 **Reader** 不等同於 Foundry agent 的 data-plane 呼叫權限。

- **SQL（連線身分）**：把這個服務主體加入資料庫並授予讀寫權限，例如：

  ```sql
  CREATE USER [<app-registration-name>] FROM EXTERNAL PROVIDER;
  ALTER ROLE db_datareader ADD MEMBER [<app-registration-name>];
  ALTER ROLE db_datawriter ADD MEMBER [<app-registration-name>];
  ```

- **Blob（本機）**：先執行 `az login`，再於儲存體帳號上將 **Storage Blob Data Contributor** 指派給該登入帳號。一般 **Contributor** 不包含 Blob data-plane 權限。`AZURE_CLIENT_*` 供 Foundry 與 SQL 使用，不會被 Blob credential 採用。

- **Blob（Azure）**：將 **Storage Blob Data Contributor** 指派給執行應用程式的 Managed Identity。

---

## 5. 啟動

首次下載專案或 `package-lock.json` 更新後，先安裝前端執行期套件：

```powershell
npm ci
```

再啟動後端與前端靜態網站：

```powershell
uv run python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 6274
```

> Agent Graph 使用 Cytoscape，檔案由 `node_modules` 掛載至 `/vendor`。若未先執行 `npm ci`，右側可能有狀態文字，但不會畫出圖形。

> 若上面這行指令因為下載套件失敗而無法啟動（而非程式錯誤），請見 [6. 疑難排解](#6-疑難排解公司網路擋住公開-pypi)。

開啟瀏覽器：<http://localhost:6274/>

---

## 6. 疑難排解：公司網路擋住公開 PyPI

### 6.1 症狀

執行 `uv run ...` 或 `uv sync` 時，程式還沒開始跑就失敗，錯誤大致如下：

```text
x Failed to build `poc-skill-generator-using-chat @ file:///C:/dev/...`
|-> Failed to resolve requirements from `build-system.requires`
|-> No solution found when resolving: `hatchling`
|-> Failed to fetch: `https://files.pythonhosted.org/packages/.../hatchling-1.32.0-py3-none-any.whl.metadata`
`-> received fatal alert: HandshakeFailure
```

### 6.2 判讀

`received fatal alert: HandshakeFailure` 是 **TLS alert 40**，代表對方**主動拒絕**這次連線，不是逾時、憑證過期或 DNS 問題。能送出這個警示的通常是網路上的攔截設備，也就是公司的 proxy / 防火牆擋住了 PyPI 的檔案 CDN。

幾個對應的重點：

- **不需要重建 `.venv`**。這與虛擬環境是否損壞無關。
- **`uv run` 每次都會連網**，因為 `pyproject.toml` 設了 `[tool.uv] package = true`，uv 會先把本專案當套件建置（需要 `hatchling`）並同步相依套件。
- **pip 正常但 uv 失敗是預期的**。uv 不讀 `pip.ini` / `PIP_INDEX_URL`，它有自己獨立的 `UV_*` 設定；同事在 pip 或 Dockerfile 裡設好的內部來源，uv 看不到。

### 6.3 解法 A：把 uv 指向公司內部套件來源（建議）

多數企業會架設內部 PyPI 鏡像 proxy。取得該網址後，設定成環境變數即可：

```powershell
# 先在當前視窗試跑，確認可行
$env:UV_DEFAULT_INDEX = "https://<你們公司的內部 pypi>/pypi/simple/"
uv sync --dry-run

# 確認沒問題後設為永久（需重開終端機才生效）
setx UV_DEFAULT_INDEX "https://<你們公司的內部 pypi>/pypi/simple/"
```

> **這一段的網址必須換成你們自己的**。以 Microsoft 內部環境為例是 `https://packagefeedproxy.microsoft.io/pypi/simple/`，但這**只是範例，並非通用設定**，對其他公司無效。請向自家 IT 詢問內部 index URL。
>
> 也有些公司不架 proxy，而是採白名單放行；那種情況要請 IT 開通 `pypi.org`、`files.pythonhosted.org`、`pythonhosted.org` 三個網域，環境變數則不必設定。

注意事項：

- `UV_DEFAULT_INDEX` 需要 uv 0.4.23 以上。較舊版本請改用 `UV_INDEX_URL`，可用 `uv --version` 確認。
- 換掉 index 之後，uv 會認定 `uv.lock` 失效並重新解析，把裡面的下載網址一併改寫成內部來源。**那份 lock 對公司網路以外的人不能用，若有版本管控請勿提交**（`git checkout -- uv.lock` 可還原）。
- 設定成環境變數而不是寫進 `pyproject.toml`，正是為了避免整個 repo 被綁死在特定公司的內部網路。

> **先跑 `uv sync --dry-run` 再決定要不要真的 sync。** 若目前的 `.venv` 是用 pip 或其他方式裝出來的，套件版本可能已經高於 `uv.lock` 的紀錄。這時候 `uv sync`（以及會隱含同步的 `uv run`）會把這些套件**降回 lock 裡的舊版本**，可能弄壞原本能跑的環境。dry-run 的輸出中以 `-` 開頭的都是會被移除或降版的套件，請先過目。若不希望變動環境，日常啟動請改用 `uv run --no-sync ...` 或下方的解法 B。

### 6.4 解法 B：直接用虛擬環境啟動（免設定的備案）

如果 `.venv` 裡的套件已經齊全，可以完全略過 uv 的建置與同步，直接啟動：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 6274
```

這與 `playwright.config.ts` 跑 E2E 測試時拉起後端的方式相同，是專案內已驗證過的路徑。等效的寫法還有 `uv run --no-sync ...`（跳過同步）與 `uv run --offline ...`（完全不連網）。

先確認虛擬環境是否完整：

```powershell
.\.venv\Scripts\python.exe -c "import uvicorn, fastapi, mssql_python; print('ok')"
```

> 這只是**臨時繞道**。日後要新增或升級套件時，uv 仍然必須連得上套件來源，屆時還是得回頭處理解法 A。

### 6.5 判斷是哪一種阻斷

用 Windows 自己的 TLS 堆疊測試，可以區分「網域被擋」與「uv 的 TLS 設定問題」：

```powershell
curl.exe -I https://pypi.org/simple/
curl.exe -I https://files.pythonhosted.org/
```

| 結果 | 判斷 | 處理 |
| --- | --- | --- |
| 兩者都不通 | 公開 PyPI 被 policy 阻斷 | 走解法 A |
| `pypi.org` 通、`files.pythonhosted.org` 不通 | 白名單漏了檔案 CDN | 請 IT 補上該網域 |
| 兩者都通、只有 uv 失敗 | uv 用 rustls，不讀 Windows 憑證存放區 | 加設 `$env:UV_NATIVE_TLS = "1"`，必要時再設 `$env:SSL_CERT_FILE` 指向公司根憑證 |

---

## 7. 登入流程（使用者操作）

1. 進入網站後，造訪 `/api/auth/login`（或前端的登入按鈕）開始 Microsoft 登入。
2. 完成 Microsoft 授權後，會自動導回並設定登入 cookie，即可開始使用。
3. 登出：呼叫 `POST /api/auth/logout`。

> 登入後系統如何把登入換成 Token 並傳進後端，請見 [03-architecture.md](03-architecture.md) 的「登入與 Token 流程」章節。
