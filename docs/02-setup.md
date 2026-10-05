# 02 - 啟動與設定指南

> 本文件帶你在**本機**跑起整個專案：先準備雲端資源並記下各項值，再填 `.env`，最後啟動。
> 專案在做什麼請見 [01-overview.md](01-overview.md)；程式碼架構與執行期行為請見 [03-architecture.md](03-architecture.md)、[04-agent-mechanism.md](04-agent-mechanism.md)。

---

## 0. 快速總覽

設定順序：

1. 安裝前置工具（[§1](#1-前置工具與外部服務)）。
2. 準備雲端資源、指派權限，並記下各項值（[§2](#2-準備雲端資源)）。
3. 複製 `.env.example` 為 `.env` 並填值（[§3](#3-填寫-env)）。
4. 啟動並登入（[§4](#4-啟動與登入)）。

下列變數全部填好，就能啟動並儲存 skill：

| 變數 | 去哪裡取得 | 準備步驟 |
| --- | --- | --- |
| `FOUNDRY_PROJECT_ENDPOINT` | Foundry 入口網站 → 專案 Overview 的 project endpoint | [2.3](#23-foundry-agent) |
| `FOUNDRY_AGENT_NAME`、`FOUNDRY_AGENT_VERSION` | 你建立的 agent 名稱與 Version | [2.3](#23-foundry-agent) |
| `MICROSOFT_TENANT_ID`、`MICROSOFT_CLIENT_ID` | 登入用 App Registration 的 Overview | [2.1](#21-登入用-app-registration) |
| `MICROSOFT_CLIENT_SECRET` | 同一支 app 的 Certificates & secrets | [2.1](#21-登入用-app-registration) |
| `MICROSOFT_OBO_SCOPE` | 同一支 app 的 Expose an API | [2.1](#21-登入用-app-registration) |
| `AZURE_TENANT_ID`、`AZURE_CLIENT_ID` | 後端用 App Registration 的 Overview | [2.2](#22-後端-service-principal) |
| `AZURE_CLIENT_SECRET` | 同一支 app 的 Certificates & secrets | [2.2](#22-後端-service-principal) |
| `AZURE_SQL_SERVER`、`AZURE_SQL_DATABASE` | Azure 入口網站 → SQL database Overview 的 Server name 與資料庫名稱 | [2.4](#24-azure-sql) |
| `AZURE_STORAGE_ACCOUNT_URL`、`AZURE_BLOB_CONTAINER` | 儲存體帳戶 → Endpoints 的 Blob service，以及容器名稱 | [2.5](#25-azure-blob) |
| `MCP_ENDPOINT` | EAA 管理員提供（`https://<eaa-host>/mcp`） | [2.6](#26-eaa-mcp) |

選用功能（TEST 路由測試、ACA 查詢與 script 型 skill、多實例）見 [§5](#5-選用功能)。

---

## 1. 前置工具與外部服務

| 工具 | 版本 | 說明 |
| --- | --- | --- |
| **Python** | 3.10 – 3.13 | 見 `pyproject.toml` 的 `requires-python` |
| **uv** | 最新 | 套件管理與執行（`uv run ...`）。若公司網路封鎖公開 PyPI，需另外指定內部套件來源，見 [6. 疑難排解](#6-疑難排解公司網路擋住公開-pypi) |
| **Azure CLI（az）** | 最新 | 本機以 `az login` 的帳號存取 Blob |
| **Node.js / npm** | 最新 LTS | 必要；執行 `npm ci` 安裝 Cytoscape、Markdown 與語法上色等前端執行期套件。未安裝時 Agent Graph 無法繪製 |

這是一個本機執行、但**依賴雲端服務**的應用：

| 服務 | 用途 | 必要 |
| --- | --- | --- |
| **Microsoft Foundry Project（Agent）** | AI 大腦：對話 orchestrator 與 PREPARE 階段的網路研究 | 是 |
| **Azure SQL Database** | 儲存 Skill metadata（`dbo.skills`）與使用者授權（`dbo.user_skill_grants`） | 是 |
| **Azure Blob Storage** | 儲存 `SKILL.md` 全文 | 是 |
| **EAA MCP** | 儲存前用 EAA 的規則檢核 skill | 是 |
| **Router runtime endpoint** | TEST 階段的路由測試 | 選用 |

使用者透過 **Microsoft Entra ID** 登入。各身分要設定什麼、給什麼權限見 [§2](#2-準備雲端資源)；每個服務實際用哪個身分、哪個 credential 見 [03-architecture.md](03-architecture.md#雲端存取身分)。

---

## 2. 準備雲端資源

需要設定的身分有三種（登入者的委派 token 不需另外設定），權限彼此獨立：

| 身分 | 需要的設定 / 權限 | 不需要 |
| --- | --- | --- |
| **登入用 App Registration（`MICROSOFT_*`）** | Web 平台 Redirect URI、client secret、暴露委派 scope；委派權限 `openid`、`profile`、`offline_access` 與自己暴露的 scope；EAA 端必須接受 `aud = api://<app-id>` | 任何 Azure RBAC、Foundry、SQL、Blob 權限；Microsoft Graph 權限；app role |
| **後端 service principal（`AZURE_*`）** | Foundry project 的 **Foundry Agent Consumer**（或 **Foundry User**）；SQL 資料庫使用者，並對 `dbo.skills`、`dbo.user_skill_grants` 具備 `SELECT` / `INSERT` / `UPDATE` / `DELETE` | Redirect URI、暴露 scope、Blob 權限、資料庫層級角色（例如 `db_datareader` / `db_datawriter`） |
| **`az login` 帳號** | 儲存體帳戶的 **Storage Blob Data Contributor** | Foundry、SQL 權限 |

> 登入用與後端用可以是同一支 App Registration（兩組變數填相同的值），但那支 app 就要同時具備上表前兩列的所有設定與權限。

### 2.1 登入用 App Registration

對應 `.env` 的 `MICROSOFT_*`。登入採 OAuth2 授權碼 + PKCE。

1. 在 Microsoft Entra 註冊一個 App Registration。Overview 頁的 **Directory (tenant) ID** → `MICROSOFT_TENANT_ID`，**Application (client) ID** → `MICROSOFT_CLIENT_ID`。
2. **Authentication** → 新增 **Web** 平台，Redirect URI 填 `http://localhost:6274/api/auth/callback`。不能用 SPA 平台：後端以授權碼換 token 時會帶 client secret。
3. **Certificates & secrets** → 新增 client secret，其值 → `MICROSOFT_CLIENT_SECRET`。
4. **Expose an API** → 設定 Application ID URI（預設 `api://<app-id>`），新增委派 scope `user_impersonation`。完整 scope → `MICROSOFT_OBO_SCOPE`，例如 `api://<app-id>/user_impersonation`。
5. 登入時請求的 scope 是 `openid profile offline_access {MICROSOFT_OBO_SCOPE}`，由使用者首次登入時同意，或由管理員事先 admin consent。使用者 email 直接取自 token claims，不呼叫 Microsoft Graph，因此不需要 Graph 權限。
6. 請 EAA 管理員確認 EAA 接受這支 app 的 audience（見 [2.6](#26-eaa-mcp)）。

> authority / authorize / token 端點由 `MICROSOFT_TENANT_ID` 自動推導，不需另外設定。登入背後的流程見 [03-architecture.md](03-architecture.md#6-登入與-token-流程)。

### 2.2 後端 service principal

對應 `.env` 的 `AZURE_TENANT_ID`、`AZURE_CLIENT_ID`、`AZURE_CLIENT_SECRET`，**Foundry 與 Azure SQL 共用**。這三個名稱是 azure-identity 的慣例：Foundry 的 `DefaultAzureCredential()` 會自動讀取它們。

1. 註冊一個 App Registration（或沿用 2.1 那支）。Overview 頁的 tenant ID → `AZURE_TENANT_ID`，client ID → `AZURE_CLIENT_ID`。
2. **Certificates & secrets** → 新增 client secret → `AZURE_CLIENT_SECRET`。
3. 不需要 Redirect URI，也不需要暴露 scope。權限在 [2.3](#23-foundry-agent) 與 [2.4](#24-azure-sql) 指派。

> Blob **不使用**這組 service principal，而是使用 `az login` 帳號（見 [2.5](#25-azure-blob)）。

### 2.3 Foundry agent

本專案以名稱 + 版本呼叫 Foundry agent，不會自動建立 agent，請先手動新增：

1. **Foundry project**：不必另建 project，可在 EAA 所使用的 Microsoft Foundry project 中新增此 agent。專案 Overview 的 project endpoint → `FOUNDRY_PROJECT_ENDPOINT`。
2. **新增空的 agent**：在 **Build → Agents** 新增 agent，名稱 → `FOUNDRY_AGENT_NAME`（例如 `skill-generator-agent`）。
3. **Model**：選 `gpt-5.6-terra` 或更新的模型。
4. **Instructions**：留空即可。每一輪對話，後端都會把 `prompts/` 組裝出的完整提示詞以 `role="system"` 訊息送出。
5. **Tools**：加入 **Web search**，讓 agent 在 PREPARE 等階段能上網研究。
6. **儲存後記下版本**：頁面右上角 **Version** 的數字 → `FOUNDRY_AGENT_VERSION`。之後在 Foundry 上修改 agent 會產生新版本，需同步更新此變數。
7. **指派權限**：將 [2.2](#22-後端-service-principal) 的 service principal 加入此 Foundry project。只呼叫既有 agent 時授予 **Foundry Agent Consumer**；需要 project data actions 時授予 **Foundry User**。一般 Azure **Owner**、**Contributor** 或 **Reader** 不等同於 Foundry agent 的 data-plane 呼叫權限。

### 2.4 Azure SQL

EAA runtime 直接從 SQL + Blob 載入 skill，因此這裡要指向 **EAA runtime 讀取的同一個資料庫**。

> **資料庫的建置與授權應該已在部署 EAA 時完成，請勿重複執行。** EAA repo 的 `skill_rbac_schema v2.sql` 建立 `dbo.skills`、`dbo.user_skill_grants` 與 `dbo.v_my_skills`；`sql-database-permission.sql` 的 Case C 以最小權限把 Skill Generator 的 service principal 加入資料庫。若不確定是否已執行，請向 EAA 管理員確認。

1. Azure 入口網站 → SQL database 的 Overview：**Server name** → `AZURE_SQL_SERVER`（例如 `your-server.database.windows.net`），資料庫名稱 → `AZURE_SQL_DATABASE`。
2. 確認 `sql-database-permission.sql` Case C 中的 service principal 名稱就是 [2.2](#22-後端-service-principal) 那一支。Case C 只授予 `dbo.skills` 與 `dbo.user_skill_grants` 的 `SELECT` / `INSERT` / `UPDATE` / `DELETE`；本專案也只需要這些。若名稱不同，請 EAA 管理員以這支 service principal 重新執行 Case C。
3. 確認 SQL Server 的防火牆允許你本機的 IP。

> 所有使用者共用這個連線身分；寫進 `dbo.user_skill_grants` 的仍是各自登入者的 email。細節見 [03-architecture.md](03-architecture.md#5-資料庫細節azure-sql--skill-rbac-schema-v22)。

### 2.5 Azure Blob

與 SQL 相同，要指向 **EAA runtime 讀取的同一個儲存體帳戶與容器**。

1. 儲存體帳戶 → **Endpoints** → Blob service 的 URL → `AZURE_STORAGE_ACCOUNT_URL`（例如 `https://youraccount.blob.core.windows.net`）；容器名稱 → `AZURE_BLOB_CONTAINER`。
2. 執行 `az login`，並在儲存體帳戶上將 **Storage Blob Data Contributor** 指派給該登入帳號。一般 **Contributor** 不包含 Blob data-plane 讀寫權限，仍會發生 403。

> Blob 驗證使用 `DefaultAzureCredential(exclude_environment_credential=True)`，刻意忽略 `AZURE_*` service principal，也不支援連線字串。

### 2.6 EAA MCP

儲存 skill 前，後端會透過 EAA 的 MCP 伺服器呼叫 `lint_skill_package`，用 EAA 自己的規則檢核。此檢核為 **fail-closed**：取不到判定就回 HTTP 503，不會儲存。

1. 向 EAA 管理員取得 MCP 根 URL → `MCP_ENDPOINT`（例如 `https://eaa.foundryeaa.org/mcp`）。
2. 請 EAA 管理員確認：EAA 驗證的 token audience 是 [2.1](#21-登入用-app-registration) 那支 app 的 `api://<app-id>`。後端以該 app 對自己做 client credentials 取得 app-only token，不需要另外設定 app role。
3. 若 EAA 驗證的是另一支 app，將那支 app 的 audience 填入 `MCP_OAUTH_AUDIENCE`。

> audience 不符時，儲存會因 lint 取不到判定而回 HTTP 503，路由測試則回 HTTP 502。lint 的完整行為見 [03-architecture.md](03-architecture.md#23-eaa-skill-lint)，呼叫身分的細節見 [03-architecture.md](03-architecture.md#24-呼叫-eaa-mcp-的身分)。

---

## 3. 填寫 `.env`

複製範本後，把 [§2](#2-準備雲端資源) 記下的值填入：

```powershell
Copy-Item .env.example .env
```

啟動時 `backend/main.py` 會 `load_dotenv(override=True)`，**`.env` 會覆蓋現有的環境變數**。下列分組與 `.env.example` 的順序一致。

### 3.1 Microsoft Foundry project

| 變數 | 必要 | 說明 |
| --- | --- | --- |
| `FOUNDRY_PROJECT_ENDPOINT` | 是 | 例如 `https://your-project.services.ai.azure.com` |
| `FOUNDRY_AGENT_NAME` | 是 | 預設 `skill-generator-agent` |
| `FOUNDRY_AGENT_VERSION` | 是 | 例如 `2` |

### 3.2 Microsoft Entra sign-in

| 變數 | 必要 | 說明 |
| --- | --- | --- |
| `MICROSOFT_TENANT_ID` | 是 | 登入用 App Registration 的 tenant ID |
| `MICROSOFT_CLIENT_ID` | 是 | 同一支 app 的 client ID |
| `MICROSOFT_CLIENT_SECRET` | 是 | 同一支 app 的 client secret |
| `MICROSOFT_OBO_SCOPE` | 是 | 例如 `api://<app-id>/user_impersonation` |

### 3.3 Foundry + Azure SQL service principal

| 變數 | 必要 | 說明 |
| --- | --- | --- |
| `AZURE_SQL_SERVER` | 是 | 例如 `your-server.database.windows.net` |
| `AZURE_SQL_DATABASE` | 是 | 資料庫名稱 |
| `AZURE_TENANT_ID` | 是 | 後端 service principal 的 tenant ID；Foundry 與 SQL 共用 |
| `AZURE_CLIENT_ID` | 是 | 同上，client ID |
| `AZURE_CLIENT_SECRET` | 是 | 同上，client secret |

### 3.4 Azure Blob storage

| 變數 | 必要 | 說明 |
| --- | --- | --- |
| `AZURE_STORAGE_ACCOUNT_URL` | 是 | 例如 `https://youraccount.blob.core.windows.net` |
| `AZURE_BLOB_CONTAINER` | 是 | 容器名稱，例如 `agent-skills`。只填容器名稱 |

### 3.5 EAA MCP

| 變數 | 必要 | 說明 |
| --- | --- | --- |
| `MCP_ENDPOINT` | 是 | EAA MCP 根 URL（`/mcp`）；未設定時無法儲存 |
| `MCP_OAUTH_AUDIENCE` | 否 | 只有 EAA 驗證的 audience 與 `MICROSOFT_OBO_SCOPE` 不同支 app 時才填 |

### 3.6 本機預設值

以下保留 `.env.example` 的值即可：

| 變數 | 預設值 | 說明 |
| --- | --- | --- |
| `SGV2_SESSION_DIR` | `./.sessions` | 撰寫中的 session JSON 存放目錄（只在 `SGV2_SESSION_STORE=local` 時有作用） |
| `SGV2_SESSION_STORE` | `local` | `local`＝存本機 JSON 檔；`blob` 見 [5.3](#53-多實例部署) |
| `SGV2_AUTH_STORE` | `local` | 登入狀態存在行程記憶體；`blob` 見 [5.3](#53-多實例部署) |

其餘變數屬於選用功能，見 [§5](#5-選用功能)。

---

## 4. 啟動與登入

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

登入：

1. 點前端的登入按鈕（或造訪 `/api/auth/login`）開始 Microsoft 登入。
2. 完成授權後會自動導回並設定登入 cookie，即可開始使用。
3. 登出：呼叫 `POST /api/auth/logout`。

---

## 5. 選用功能

### 5.1 路由測試

TEST 階段把正 / 負面範例送到 runtime，看 Router 是否路由到本 skill。不設定時，其他流程照常運作，只是無法跑路由測試。

| 變數 | 說明 |
| --- | --- |
| `SKILL_SELECTION_TEST_RUN_URL` | runtime 的 `/run` 端點。任何實作 `/run` 契約的端點都可以；可直連 EAA，也可經由 APIM 等閘道 |

```dotenv
SKILL_SELECTION_TEST_RUN_URL=https://eaa.foundryeaa.org/run
# 經由 APIM 等閘道時
#SKILL_SELECTION_TEST_RUN_URL=https://example-apim.azure-api.net/coding-tool-apis/run
```

- 路由測試送的是**登入者的委派 token**，只看得到登入者被授權的 skill。儲存時會自動 grant 給儲存者，請用同一個帳號儲存與測試。
- 整批回 HTTP 502 且提到 token 被拒時，請聯絡 EAA 管理員查 ACA log 的 `[oauth] Token rejected:`，並確認 [2.6](#26-eaa-mcp) 的 audience 設定。
- runtime 必須符合的契約見 [04-agent-mechanism.md](04-agent-mechanism.md#34-testrouter-endpoint-路由盲測)。

### 5.2 ACA 環境變數查詢與 script 型 skill

設定後，PREPARE 階段會透過 EAA MCP 讀取 EAA 所在 Azure Container Apps（ACA）應用**目前已有的環境變數**與 **OBO scope 註冊表**，讓 Agent 分辨 skill 變數是「ACA 已有（reuse）」或「需新增（add）」。值請向 EAA 管理員取得。

| 變數 | 說明 |
| --- | --- |
| `ACA_APP_NAME` | EAA 的 Container App 名稱 |
| `ACA_RESOURCE_GROUP` | 該 app 的資源群組 |
| `ACA_SUBSCRIPTION_ID` | 該 app 的訂閱 ID |

- 這三個加上 `MCP_ENDPOINT` **四個都填才會啟用**；任一留空就不查詢，其他流程不受影響。
- **Script 型 skill**（`SKILL.md` 加上一支 `scripts/<name>.py`）需要這個查詢。只有當 ACA 上 `DYNAMIC_SKILLS_ENABLED` 與 `SKILL_SCRIPTS_ENABLED` 都是 `true` 時才會產出 script 型；未啟用查詢時一律是 inline。旗標如何影響 PREPARE 與儲存，見 [04-agent-mechanism.md](04-agent-mechanism.md#eaa-script-旗標)。

### 5.3 多實例部署

> **注意：本 Repo 實際以本機（`SGV2_SESSION_STORE=local`、`SGV2_AUTH_STORE=local`）測試為主。** 以下 Blob 模式的程式已開發完成，但**尚未實際部署測試**；啟用前請自行驗證。

預設 session 存本機檔案、登入狀態只存在**行程記憶體**，兩者都不跨實例共享。要跑多個後端實例時，把兩者改存 Blob，否則使用者被路由到不同實例時會看不到自己的 session 或被登出。

只要改這兩行，就會沿用 `AZURE_STORAGE_ACCOUNT_URL` 與 `AZURE_BLOB_CONTAINER`：

```dotenv
SGV2_SESSION_STORE=blob
SGV2_AUTH_STORE=blob
```

要分開存放時才需要下列變數：

| 變數 | 預設值 | 說明 |
| --- | --- | --- |
| `SGV2_SESSION_BLOB_CONTAINER` | 沿用 `AZURE_BLOB_CONTAINER` | session 專用容器 |
| `SGV2_SESSION_BLOB_PREFIX` | `sessions` | session blob 前綴；不像 skill 的 `skills/` 前綴受 SQL 計算欄位限制，可自由改名 |
| `SGV2_AUTH_STORAGE_ACCOUNT_URL` | 沿用 `AZURE_STORAGE_ACCOUNT_URL` | auth 專用儲存體帳戶 |
| `SGV2_AUTH_STORAGE_CONNECTION_STRING` | 沿用 `AZURE_STORAGE_CONNECTION_STRING` | 改用連線字串驗證時才設；設了就優先於帳戶 URL |
| `SGV2_AUTH_BLOB_CONTAINER` | 依序沿用 `SGV2_SESSION_BLOB_CONTAINER`、`AZURE_BLOB_CONTAINER` | auth 專用容器 |
| `SGV2_AUTH_BLOB_PREFIX` | `auth` | auth blob 前綴 |

寫入位置分別是 `<container>/sessions/<owner_upn>/<session_id>.json` 與 `<container>/auth/<kind>/<key>.json`，與 skill 的 `skills/` 前綴互不重疊。

> session 與 auth 的 Blob 使用 `ChainedTokenCredential(ManagedIdentity, AzureCli)`，同樣不採用 `AZURE_*` service principal，權限需求與 [2.5](#25-azure-blob) 相同。

> **目前限制**：Blob 模式沒有自動化測試（`tests/` 只涵蓋本機儲存），且 `POST /api/e2e/reset` 會強制切回本機 session 儲存，因此不能用 E2E reset 驗證 Blob 模式。

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

## 附錄 A：改寫為雲端版本（本 repo 未實測）

> **本 repo 是本機執行版本。** 客戶可依自己的實務需求，將它改寫並部署至 Azure Container Apps、App Service 等雲端環境；但本 repo **未實測任何雲端部署**，也不提供 Dockerfile / IaC / CI。下面列的是審視程式碼後，改寫時需要處理的事項，供參考；清單未必完整。

**架構上本來就可行的部分**：前端是純靜態 HTML/CSS/ES module（無 build step），由 `backend/main.py` 自己掛載後提供：`/` → `frontend/index.html`、`/assets` → `frontend/`、`/vendor` → `node_modules/`。因此**不需要另一個前端主機服務**，一個容器同時 serve 前端與 API 即可。

**雲端上的身分（依程式碼推導）**：Foundry 與 SQL 仍使用 `.env` 的 `AZURE_*` service principal，不會自動改用 Managed Identity；Blob 沒有 `az login` 可用，會落到應用程式的 Managed Identity，需將 **Storage Blob Data Contributor** 指派給它（另見下方第 1 點）。

### 改寫時需處理的事項

**1. Blob 的 Managed Identity 拿不到 token（需改程式，無法用設定繞過）**

容器裡沒有 `az login`，`backend/blob_store.py` 的 `DefaultAzureCredential(exclude_environment_credential=True)` 只能落到 `ManagedIdentityCredential`。但 azure-identity 的 `DefaultAzureCredential` 會把 `AZURE_CLIENT_ID` 當成 **user-assigned MI 的 client id**：

```python
# azure/identity/_credentials/default.py
managed_identity_client_id = kwargs.pop(
    "managed_identity_client_id", os.environ.get(EnvironmentVariables.AZURE_CLIENT_ID)
)
```

而本專案的 `AZURE_CLIENT_ID` 是 SQL / Foundry 那支 **App Registration**，不是任何 UAMI，所以 MI 端點會被要求發一個不存在的身分的 token 而失敗，導致所有 `SKILL.md` 讀寫壞掉。這**不能靠調整環境變數解決**（App Registration 與 UAMI 是不同物件，client id 不可能相同），必須改程式明確指定要用哪個 MI。

附帶一個不一致：`backend/session_store.py` 與 `backend/auth_store.py` 用的是明寫的 `ManagedIdentityCredential()`（不帶 client id），走 **system-assigned**，與 skill store 的行為不同。

**2. uvicorn 綁定位址**

[4. 啟動與登入](#4-啟動與登入) 的指令是 `--host 127.0.0.1`。容器內這樣綁，ingress 從外面連不進來，健康檢查直接失敗。需改 `--host 0.0.0.0` 並與 ingress 的 `targetPort` 對齊。

**3. Redirect URI 與反向代理**

App Registration 要加上正式網域的 `https://<fqdn>/api/auth/callback`。另外 `public_url_for()` 直接用 `request.url_for` 組 redirect_uri；TLS 在 ingress 終止，若未處理 `X-Forwarded-Proto` 會組出 `http://…` 而與 Entra 註冊值不符——啟動時需帶 `--proxy-headers --forwarded-allow-ips=…`。

**4. 登入 cookie 的 `secure` 旗標**

`set_cookie` 目前寫死 `secure=False`（本機 http 專用），HTTPS 上應改為 `True`。

**5. 映像檔內容**

必須包含 `frontend/` 與**完整的 `node_modules/`**（`/vendor` 掛的是 node_modules 本身，不是打包產物）。兩個掛載都是條件式的，**缺了不會報錯**：`frontend/` 缺 → `/` 回 404 `Frontend not built`；`node_modules/` 缺 → `/vendor` 靜默不掛載，頁面出得來但 Agent Graph 不畫圖。

**6. session 與登入狀態**

容器檔案系統是 ephemeral，且登入 token 預設只在行程記憶體，需依 [5.3 多實例部署](#53-多實例部署) 改成 Blob——但那一段本身也只在本機跑過。

**7. 確認 `SGV2_E2E_MODE` 沒設**

預設就是關的，但別跟著本機 `.env` 一起帶進正式環境——`POST /api/e2e/reset` 會清資料。
