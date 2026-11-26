# Scenario 與多 capability 設計指引

這份文件供技能作者設計多個能力技能協作的情境使用，不是 Generator prompt。

## 一、前提

- **Host owner 的 scenario**：只有宿主讀取 parent 正文。後端不讀 parent 正文，只依 parent 的 `children` 白名單載入可用技能。
- 宿主以 `run_coding_workflow(skill="<scenario 名稱>")` 委派，每一輪都重帶同一個 `skill`。`scenario` 參數是舊入口，不使用。
- `children` 是白名單，不是執行順序。後端模型依 `request` 與各 child 的 `description` 決定載入哪些技能；runtime 不提供 child 之間的角色名稱對照。

## 二、設計規則

1. **Owner 的選擇。** 預設 `host`。只有把編排決策交給 EAA 能解決已存在且可觀察的問題時（例如多個宿主共用同一套接續規則），才選 `eaa`。不能只因 children 都在 EAA 執行就選 `eaa`。不需要宿主專屬能力的 Host scenario 是合法的，宣告為 `**宿主專屬能力：** 無`。
2. **能力技能不互相呼叫，也不在正文指名彼此。** 以共用的 caller-owned 鍵（例如檔名、案件編號）表達依賴，各技能都從 `request` 取得同一個值。前一步把中間產物寫到 `_debug/` 下由該鍵推算出的約定路徑；後一步依同一約定讀取。找不到前置產物時，回報流程尚未完成或下游錯誤，不向使用者索取流程自己產生的中間產物。
3. **一次委派內的順序。** `script` 型技能先透過工具呼叫執行，`inline` 型技能只能放在最後一步。`inline` 程式以 `SUCCESS` 結束時，本輪也結束，不會再回到模型呼叫其他工具。
4. **多次委派。** `_debug/` 只在 session 開始時清除，跨輪保留，所以後一次委派可依約定路徑讀取前一次的產物。`request` 只帶 caller-owned 的鍵，不轉傳中間結果。宿主 instruction 要求將 `completed` 原樣呈現，目前沒有「中間步驟不呈現」的例外；採用多次委派前要先處理這一點。
5. **S4 寫法。** `Actor` 每列只能是 `Host` 或 `Backend`。`Backend` 列只寫業務結果，不寫哪支 child、檔案路徑或呼叫順序。表格上方的宿主專屬能力宣告，名稱須與 PREPARE 記錄的 `host_capabilities` 相同；EAA MCP 工具不算宿主專屬能力。

## 三、範例：需求清單格式檢查（一次委派）

- **Checker（`script`）**：以 `--file <需求檔>` 執行，將檢查結果印到 stdout，並寫到 `_debug/req-check/<需求檔檔名不含副檔名>.check.json`。缺少 `--file` 時回傳 `[NEEDS_INFO]`，不寫檔。
- **Reviewer（`inline`，最後一步）**：以同一個需求檔名為鍵，讀取需求檔及約定路徑上的結果檔，產出問題表與需使用者確認的改寫建議。找不到結果檔時，說明格式檢查尚未完成，不向使用者索取中間產物。
- **Scenario（Host owner）**：宿主不取 children 正文，`request` 只帶需求檔名。S4 如下：

  **宿主專屬能力：** 無

  | 步驟 | 執行者 | 工作 |
  | --- | --- | --- |
  | 1 | Host | 確認有需求檔名。 |
  | 2 | Backend | 檢查格式並產生改寫建議。 |
  | 3 | Host | 依回傳分支處理；`completed` 原樣呈現。 |

**常見錯誤**：讓 `inline` child 先寫檔，再呼叫 `script` child，在一輪內無法完成（違反規則 3）；在 child 正文用角色名稱指稱另一支 child（違反規則 2）。

## 四、依據與限制

- **規則 1**：依據 2026-10-20 的 owner 對照實驗紀錄。這是單次實驗，不是效能基準。
- **規則 2–4**：依據對 EAA runtime 程式碼的唯讀分析，包括 `run_skill_script` 的工具範圍、`inline` 以 `SUCCESS` 結束本輪、`work_dir`／`_debug/` 的保留行為，以及 Host 模式不注入 parent 正文。相關唯讀快照見 [skills_provider_factory.py](../reference/skills_provider_factory.py) 與 [core_handler.py](../reference/core_handler.py)。
- 上述是程式碼分析結果，範例尚未實際上架驗證。後端模型是否依預期順序呼叫工具，無法由程式碼保證。
