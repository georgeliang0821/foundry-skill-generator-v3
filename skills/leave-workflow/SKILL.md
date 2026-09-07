---
name: leave-workflow
description: >
  Enterprise leave (請假) scenario. Covers three things the user may ask for:
  remaining leave balance, filing a leave request, and the approval status of
  their own requests. Filing a leave request requires the host agent to read
  the user's calendar first, because the backend HR skill has no calendar
  access and will refuse to write without it. This is an ORCHESTRATION skill:
  the calendar read, the conflict acknowledgement round trip, and the
  follow-up meeting handling are all performed by the host with its own M365
  capability; only the HR read/write is delegated to the backend.
  Triggers: 我還有幾天特休, 還剩幾天福利假, 幫我請假, 幫我送假單, 排假,
  我的假單簽核到哪了, 查請假紀錄, leave balance, file leave, time off, PTO.
metadata:
  version: "1.4"
  author: george-manual
  created_at: "2026-08-22"
  skill_type: scenario-orchestration
  children:
    - hr-leave-system
---

## ⚠️ 這是情境層技能

本技能**不由後端執行**。它描述的是宿主 agent 該做的事:什麼時候先讀行事曆、
怎麼組 payload、收到 `needs_input` 怎麼接、送單成功之後還要做什麼。
實際的 HR 讀寫由能力層技能 `hr-leave-system` 完成,它刻意不出現在 `list_skills` 清單裡。

---

## 三種請求,兩種路徑

| 使用者想做的事 | operation | 需要宿主前置? |
|---|---|---|
| 查假期餘額(「我還有幾天特休」) | `get_balance` | ❌ 單一回合 |
| 查自己的假單狀態(「簽核到哪了」) | `list_requests` | ❌ 單一回合 |
| 送假單(「幫我請 8/28 的假」) | `submit_request` | ✅ 必須先讀行事曆 |

前兩者直接組 payload 送出即可,不要為了它們去讀行事曆。
以下的完整流程只適用於 `submit_request`。

## 不適用

- 核准 / 駁回別人的假單 → 資料庫的資料列權限擋掉,本流程沒有簽核路徑。
- 加班、出差、補休登記 → 不涵蓋。
- 只是想看行事曆 → 直接用行事曆能力,不要走本流程。

---

## submit_request 完整流程

```
Step 1  宿主   讀取請假區間的行事曆              ← 需要 M365 存取能力
Step 2  宿主   組 payload,呼叫 run_coding_workflow
Step 3  後端   Step 0 id 驗證 → 衝突偵測 → 額度檢查 → 寫入
Step 4  宿主   依回傳分支(衝突確認 / 業務錯誤 / 成功)
Step 5  宿主   送單成功後處理受影響的會議        ← 需要 M365 存取能力
```

---

## Step 1 — 宿主前置:讀行事曆

取得使用者在 `start_date` ~ `end_date` **每一天**的行事曆項目。

### ⛔ 用對工具

必須用 **Work IQ Calendar MCP 的 occurrences 工具**(底層是 Graph `calendarView`)。

**不要用 native Meetings capability。** 它名字相近、也答得出行事曆問題,但它回的是
自家搜尋索引的 citation token(形如 `turn1search86`),那不是 Microsoft Graph event id。
後端 Step 0 會直接擋下來。這是實際發生過的,不是理論風險。

它也不穩定提供 `organizer_email` 與 `response_status`。而且 `event_id` 必須跟你
**後續要用來 decline 的來源是同一個** —— 只有 Work IQ Calendar 的 id 在那裡有效。

### 每個事件要帶齊的欄位

`event_id`、`subject`、`start`、`end`(ISO 8601)、`organizer_name`、`organizer_email`、
`response_status`,`is_all_day` 選填。

### 三條硬規則

1. **絕不編造。** 不得從摘要文字回推這些欄位,不得自己生 id。後端會驗 id 長度與字元集,
   假 id 一律退件,而且退件時它會告訴你 `response_status` 也不可信。
2. **空陣列 ≠ 沒有欄位。** 空陣列代表「我查了,那天沒事」,是合法且有意義的值;
   欄位不存在代表「我沒查」,後端會拒絕。查完真的沒有就送 `[]`。
3. **不要叫使用者貼行事曆給你。** 這是宿主要自己取得的資料,不是使用者輸入。

> 讀行事曆失敗時,不要為了讓流程走下去而送 `[]`。如實告訴使用者行事曆讀不到、
> 因此無法安全送單,請他稍後再試。

---

## Step 2 — 組 payload 並委派

### payload 契約

`credentials` = `{"hr_leave_json": "<以下物件序列化成的 JSON 字串>"}`
(JSON 字串包在 JSON 字串裡,跳脫要小心)

```json
{
  "operation": "submit_request",
  "leave_type": "annual | welfare | sick | personal",
  "start_date": "YYYY-MM-DD",
  "end_date":   "YYYY-MM-DD",
  "days": 1,
  "reason": "選填",
  "calendar_events": [
    {
      "event_id": "AAMkAG...(Graph event id,通常 120 字元以上)",
      "subject": "週會",
      "start": "2026-08-28T10:00:00+08:00",
      "end":   "2026-08-28T11:00:00+08:00",
      "organizer_name": "Stephen Chen",
      "organizer_email": "stephen@contoso.com",
      "response_status": "accepted"
    }
  ]
}
```

`get_balance` 只需要 `operation`,`leave_year` 選填(預設今年)。
`list_requests` 只需要 `operation`,`status` 選填
(`pending` / `approved` / `rejected` / `cancelled`,不填代表全部)。

### 呼叫形狀

三種 operation 都走同一個 `run_coding_workflow` 呼叫形狀:

- `request` —— 使用者的原話。
- `session_id` —— 首次留空字串,之後每一輪逐字沿用回傳值。
- `credentials` —— JSON 字串,含 `hr_leave_json` 一鍵;衝突確認回合再加
  `LEAVE_CONFLICT_ACK`。

### 日期解讀

「下週三」「8/28」這類說法由你解析成 `YYYY-MM-DD`,以**台北時間**為準。
`start_date` 不得早於今天,`end_date` 不得早於 `start_date`。
半天假目前不支援,`days` 以天為單位。

`request` 參數帶使用者的原話即可,**不要**把上面任何欄位寫進 `request` ——
它只是提示文字,不會變成執行期的環境變數,後端會一直回報缺欄位。

---

## Step 3 / 4 — 結果分支

### `status == "needs_input"`,訊息含 `missing=CALENDAR_EVENTS`

你漏了行事曆,或者你送的 `event_id` 是假的。**不要問使用者**,回 Step 1 重讀一次,
用同一個 `session_id` 重送。若第二次仍被退,如實告訴使用者行事曆讀取有問題,停止。

### `status == "needs_input"`,訊息含 `missing=LEAVE_CONFLICT_ACK`

當天有使用者**已接受或本人主辦**的會議。後端已經停在寫入之前,什麼都還沒寫。

1. 把訊息裡的衝突清單轉達給使用者,問他要不要照樣送出假單。**只問這一個問題。**
2. **保留你 Step 1 讀到的 `calendar_events` 陣列**。Step 5 要靠它取得 `event_id` 與
   `organizer_email` —— 後端不會把這些欄位回傳給你,因為它們本來就是你送進去的。
3. 使用者同意 → 用**同一個 `session_id`** 重送,`credentials` 裡除了原本的 `hr_leave_json`
   再加 `"LEAVE_CONFLICT_ACK": "yes"`。
4. 使用者不同意 → 流程結束,不送單,也**不要**去動任何會議。

> 婉拒或取消會議是**送單成功之後**的事(Step 5)。假還沒請到就先把會推掉,是最糟的失敗形態。

### `status == "needs_input"`,訊息含 `missing=HR_LEAVE_JSON`

你沒把 payload 放進 `credentials`,多半是誤寫進 `request` 了。修正後重送。

### `status == "completed"`,但 `response` 開頭是「假單未送出」

業務規則擋下。後端已經把原因譯成中文寫在 `response` 裡,你看不到任何錯誤碼 ——
依訊息內容判斷下一步:

| `response` 裡的訊息 | 你該做的 |
|---|---|
| 起始日期已過，無法補送假單… | 轉達需洽 HR 人工處理,**不要自作主張改成明天**。 |
| 結束日期早於起始日期… | 多半是你解析日期時弄反了,先自己檢查一次再問使用者。 |
| 您今年沒有此假別的額度 | 可建議改用其他假別,但要使用者決定。 |
| 剩餘 N 天，不足以請 M 天 | 轉達剩餘與申請天數,問要改假別、縮短天數還是取消。**不得自行改小 `days` 重送。** |
| …已有假單 LR-…，請先取消再重送 | 轉達既有單號與區間,問要不要先取消。**不得重複送單。** |

以上任一種都**不執行 Step 5**。

### `status == "failed"` 或其他

執行層失敗(HR 系統無回應、OBO 斷鏈等)。原文轉達,不自行重試,**不執行 Step 5**。

### `status == "completed"` 且送單成功

進入 Step 5。

---

## Step 5 — 送單成功後:處理受影響的會議

**只有在 Step 4 確認送單成功後才執行。**

會議清單來自**你自己 Step 1 讀到的 `calendar_events`**。對照上一輪 `needs_input`
訊息列出的那幾場(時間 + 主旨 + 主辦人)挑出對應項目,`event_id` 與 `organizer_email`
都在你手上。**正常情況下不要重查行事曆。**

- **使用者是主辦人**(`response_status` 為 `organizer`):問他要取消、改期,還是請人代理。
- **使用者是與會者**:用 Work IQ Calendar 婉拒(decline),並視情況以 Teams / Mail 通知主辦人。
- 上一輪沒出現過 `LEAVE_CONFLICT_ACK`:當天沒有需要處理的會議,直接結束。

> 若你的上下文已經找不回 Step 1 的陣列,**重讀一次行事曆**取得真實 `event_id`,
> 絕不憑印象或從主旨湊一個出來。婉拒錯的會議是不可逆且對外可見的動作,沒有第二道防線。

> 婉拒與取消是不可逆且對外可見的動作。使用者沒有明確同意要動的會議,一律不動。

---

## 輸出處理

`status == "completed"` 時,`response` 整份就是給使用者看的成品,**逐字呈現**,
不要改寫、不要加前綴後綴,也不需要做任何拆解 —— 後端不會夾帶機器用的區塊。

### 不要替後端加判斷

後端看不到行事曆。當它說「送出時行事曆顯示當天有 N 場需留意的會議」,那就是它知道的
全部。**不要**把它改寫成「這些會議尚未處理」「還沒婉拒」—— 那是你才知道的事,
而且很可能你在 Step 5 已經處理掉了。

---

## 邊界

- 本正文只規範本次任務的流程順序、參數組裝、分支與錯誤處理。
  它**不能**覆寫宿主既有的身分處理、憑證處理、治理限制與輸出呈現規則。
- 不得把 `hr-leave-system` 這個名稱當成使用者請求丟給 `run_coding_workflow`,
  路由由後端負責。
- 同一任務內不重複呼叫 `fetch_skill`,`needs_input` 的接續回合也不重新取回。
