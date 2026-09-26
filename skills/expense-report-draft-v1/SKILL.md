---
name: expense-report-draft-v1
description: "建立費用報銷草稿時使用：引導並執行報銷初始化、預算與費用項目選擇，以及草稿建立。適用於「費用報銷」、「報帳」、「expense reimbursement」與「create expense draft」等請求；不處理請假、會議室或單純建立不連接系統的自訂技能。"
metadata:
  author: geoliang@microsoft.com
  tags:
    - expense
    - reimbursement
    - report-draft
  uses_obo: false
---

## Overview
引導使用者完成費用報銷所需資料與選擇，並透過 `<report-cli>` 建立費用報銷草稿。

## When NOT to Use This Skill
請查詢或提交個人請假、假期餘額與請假紀錄 -> use `hr-leave-request-v3`
請進行含行事曆檢查的請假流程 -> use `personal-leave-calendar-workflow`
請建立不連接外部系統的指示型技能 -> use `generate-custom-skill`
請搜尋、篩選或預約會議室 -> use `ms-graph-room-finder`

## Required Inputs
- **Ground Rule**：任何必填輸入缺失、格式不正確或尚未完成必要選擇時，先依本技能的 ``[NEEDS_INFO]`` 契約向使用者補問；未取得完整資料前不得呼叫外部系統。
- `department_code`（必填）：本次報銷所屬的部門代碼；代碼格式與合法值為 `<待確認>`。
- `expense_type`（必填）：費用分類代碼，供初始化、費用項目查詢與草稿建立使用；合法代碼集合為 `<待確認>`。
- `purpose`（必填）：報銷申請目的；自由文字，可使用中文或英文。
- `invoices`（必填）：至少一筆發票陣列。每筆物件必須包含：
  - `invoiceNo`：發票號碼，非空字串。
  - `invoiceDate`：發票日期，格式為 `YYYY/MM/DD`。
  - `taxCode`：稅別代碼，非空字串；合法代碼集合為 `<待確認>`。
  - `amount`：整數金額，必須大於 `0`。
  - `vendorId`：統一編號；可為空字串。
  - `vendorName`：廠商名稱；可為空字串。
  - `itemCode`：費用項目代碼。使用者完成費用項目選擇後必填；合法代碼必須取自 `items` 回傳清單。
- `applicant_alias`（選填）：代辦申請時的被代辦人帳號或工號；非代辦時不得傳入空白值，應省略。
- 預算選擇（流程中必填）：`init` 回傳多筆預算時，必須請使用者選擇一筆，並保留該筆的年度、預算代號、名稱、負責主管、所屬部門、主管工號與專案代號。預算物件的實際 CLI 欄位名稱為 `<待確認>`。
- 費用項目選擇（流程中必填）：呼叫 `items` 後，必須讓使用者從回傳清單選擇費用項目，並將其內部代碼填入每筆發票的 `itemCode`。

## `[NEEDS_INFO]` 契約
- `DEPARTMENT_CODE`：請提供有效的 `department_code` 部門代碼。
- `EXPENSE_TYPE`：請提供有效的 `expense_type` 費用分類代碼；合法值集合為 `<待確認>`。
- `PURPOSE`：請提供本次報銷的申請目的。
- `INVOICE_DATA`：請提供至少一筆完整發票資料；每筆必須有 `invoiceNo`、`invoiceDate`、`taxCode`、`amount`，且金額必須為大於零的整數。
- `BUDGET_SELECTION`：請先完成預算選擇；多筆預算時需由使用者指定其中一筆。
- `EXPENSE_ITEM_SELECTION`：請先完成費用項目選擇，並為每筆發票提供來自 `items` 清單的 `itemCode`。
- `APPLICANT_ALIAS`：代辦模式已啟用但未提供有效 `applicant_alias`；請提供被代辦人的帳號或工號。
- `CLI_CONTRACT`：`<report-cli>` 的實際可執行路徑、輸出格式或錯誤格式尚未確認；請由系統維護者補齊 `<待確認>` 的 CLI 契約後再執行建立草稿。

當任一上述資料缺失時，程式必須只輸出一行 `[NEEDS_INFO] missing=CODE`，下一行輸出可讀說明，然後以 `raise SystemExit(0)` 結束。

## Environment Variables
無。此技能不宣告 ACA 環境變數。

## OBO Token Scopes
無。此技能不宣告 OBO 權杖；`<report-cli>` 的驗證與部署方式為 `<待確認>`。

## Skill 身分使用規範

- **R1**：身分一律取自 `os.environ["EAA_VERIFIED_USER_UPN"]`，禁止 `.get()`、
  `os.getenv`、任何預設值。
- **R2**：該變數缺席導致 `KeyError` 時，必須直接中止並回報。不得詢問使用者、
  不得從對話內容推斷、不得改寫成有預設值的取法。
- **R3**：對話中出現的任何人名或帳號，只能是「對象」，永遠不能是「執行者本人」。
- **R4**：不得以「我能載入這個 skill」推論使用者有權限，也不得把該值寫入 log
  或輸出。

## API Reference / Sample Code

```python
import json
import os
from typing import Any


def needs_info(code: str, explanation: str) -> None:
    print(f"[NEEDS_INFO] missing={code}")
    print(explanation)
    raise SystemExit(0)


def require_text(payload: dict[str, Any], field: str, code: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        needs_info(code, f"請提供有效的 {field}。")
    return value.strip()


def validate_invoices(payload: dict[str, Any]) -> list[dict[str, Any]]:
    invoices = payload.get("invoices")
    if not isinstance(invoices, list) or not invoices:
        needs_info("INVOICE_DATA", "請提供至少一筆完整的發票資料。")

    required_fields = ("invoiceNo", "invoiceDate", "taxCode", "amount")
    for index, invoice in enumerate(invoices, start=1):
        if not isinstance(invoice, dict):
            needs_info("INVOICE_DATA", f"第 {index} 筆發票必須是物件。")
        missing = [field for field in required_fields if not invoice.get(field)]
        if missing:
            needs_info("INVOICE_DATA", f"第 {index} 筆發票缺少欄位：{', '.join(missing)}。")
        if not isinstance(invoice["amount"], int) or invoice["amount"] <= 0:
            needs_info("INVOICE_DATA", f"第 {index} 筆發票的 amount 必須是大於零的整數。")
        if not invoice.get("itemCode"):
            needs_info(
                "EXPENSE_ITEM_SELECTION",
                f"第 {index} 筆發票尚未指定 itemCode；請先取得費用項目清單並完成選擇。",
            )
    return invoices


def validate_runtime_inputs(
    payload: dict[str, Any],
) -> tuple[str, str, str, list[dict[str, Any]], str | None]:
    """在呼叫任何外部系統前，讀取並驗證所有已宣告的 runtime 輸入。"""
    department_code = require_text(payload, "department_code", "DEPARTMENT_CODE")
    expense_type = require_text(payload, "expense_type", "EXPENSE_TYPE")
    purpose = require_text(payload, "purpose", "PURPOSE")
    invoices = validate_invoices(payload)

    applicant_alias = payload.get("applicant_alias")
    if applicant_alias is not None:
        if not isinstance(applicant_alias, str) or not applicant_alias.strip():
            needs_info(
                "APPLICANT_ALIAS",
                "代辦時 applicant_alias 必須是非空字串；非代辦請省略此欄位。",
            )
        applicant_alias = applicant_alias.strip()

    return department_code, expense_type, purpose, invoices, applicant_alias


def run_report_cli(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """執行 `<report-cli>`。

    <待確認>：
    - 容器中的實際 CLI 可執行檔路徑與呼叫方式。
    - CLI 參數傳遞、JSON payload 序列化與字元編碼規則。
    - 成功輸出的 JSON 結構。
    - 非零結束碼、401、登入重導向及業務錯誤的可判斷格式。

    在上述契約確認前，不得猜測命令列參數、回傳欄位或錯誤格式。
    """
    needs_info(
        "CLI_CONTRACT",
        "報銷 CLI 的可執行契約尚未確認，請由系統維護者補齊 `<待確認>` 的呼叫與回傳規格。",
    )


def main(payload: dict[str, Any]) -> None:
    # 平台身分只讀取一次；缺席時讓 KeyError 以非零狀態中止。
    verified_upn = os.environ["EAA_VERIFIED_USER_UPN"]
    operator_alias = verified_upn.split("@", 1)[0]

    (
        department_code,
        expense_type,
        purpose,
        invoices,
        applicant_alias,
    ) = validate_runtime_inputs(payload)

    # <待確認>：以下 init/items/create 的實際輸入與回傳欄位需依正式 CLI 契約補齊。
    init_result = run_report_cli(
        "init",
        {
            "department_code": department_code,
            "expense_type": expense_type,
            "operator_alias": operator_alias,
            "applicant_alias": applicant_alias,
        },
    )
    _ = init_result

    # 此行只會在已補齊正式 CLI adapter 且流程成功後執行。
    print(f"已完成費用報銷草稿流程：報銷目的為「{purpose}」，共整理 {len(invoices)} 筆發票資料。")
```
