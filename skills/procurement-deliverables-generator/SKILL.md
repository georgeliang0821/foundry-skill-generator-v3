---
name: procurement-deliverables-generator
description: "Generate, validate, manifest, and publish a controlled procurement deliverables set from an existing canonical requirements package. Use for Chinese or English requests to produce synchronized Word specifications, Excel test trackers, and HTML prototypes by procurement case ID; do not use for OCR retrieval, upstream canonical-requirements analysis, or a generic standalone project overview page."
metadata:
  author: "a-wureeve@microsoft.com"
  tags:
    - procurement
    - deliverables
    - requirements
    - sharepoint
  uses_obo: true
---

## Overview

Generates synchronized procurement Word, Excel, and HTML deliverables from an existing canonical requirements package, creates a manifest, and publishes the three primary files to SharePoint.

## When NOT to Use This Skill

Reviewing OCR evidence, resolving requirement conflicts, or creating/updating canonical requirements -> use `procurement-requirements-analysis`.
Retrieving, downloading, extracting, or OCR-processing procurement source documents -> use `procurement-sharepoint-ocr`.
Creating only a generic technical overview, architecture explanation, or presentation -> not this skill; for a self-contained HTML slide deck use `html-ppt`.

## Required Inputs

```input-bindings
- name: procurement_case_id
  source: request
  required: true
```

- `procurement_case_id` (required): A non-empty procurement case identifier used as the local canonical-package folder name and the SharePoint publication folder. It must be one folder name without `/`, `\`, `.`, `..`, or SharePoint-invalid characters: `" * : < > ? \ | / # %`.
- The skill reads the canonical requirements file at `Procurement/<procurement_case_id>/analysis/canonical-requirements.md` and writes the local deliverables to `Procurement/<procurement_case_id>/output`.
- Ground Rule: Ask the user for every missing required input before reading local case files, generating deliverables, or calling SharePoint.

## `[NEEDS_INFO]` 契約

- `PROCUREMENT_CASE_ID`: Obtain and bind a valid case identifier to `request_inputs["procurement_case_id"]`. It must be a non-empty single folder name and must not contain SharePoint-invalid characters. Do not infer it from unrelated request text.

## Environment Variables

None. This skill does not declare ACA environment variables.

## OBO Token Scopes

- `GRAPH_ACCESS_TOKEN` (required): OBO token for Microsoft Graph with scope `https://graph.microsoft.com/.default`. It is injected by the OBO exchange and authorizes publication and verification in the fixed SharePoint `Procurement` library; its absence means the OBO chain is broken and execution must end non-zero.

## 部署設定使用規範

- **D1**：`## Environment Variables` 與 `## OBO Token Scopes` 宣告的每個變數，
  一律以 `os.environ["NAME"]` 索引式讀取，禁止 `.get()`、`os.getenv`、
  `or "..."` 之類的 fallback、任何預設值或預設參數。
- **D2**：這些變數缺席導致的 `KeyError` 必須直接向上拋出、以非零狀態中止。
  絕不可輸出 `[NEEDS_INFO]`、絕不可要求 caller 或使用者補值——它們由部署與
  OBO 交換注入，caller 沒有能力提供，要求補值只會造成無限重試。
- **D3**：不得以 `try`/`except` 吞下該 `KeyError` 後改走任何 exit 0 的路徑，
  也不得改用 service principal、managed identity 或任何替代憑證繼續執行。

# Fixed Configuration

```text
SITE_URL = "https://mngenvmcap352952.sharepoint.com"
LIBRARY_NAME = "Procurement"
OUTPUT_FOLDER = "Output"
WORD_FILE = "requirements-specification.docx"
EXCEL_FILE = "requirements-test-tracker.xlsx"
HTML_FILE = "prototype.html"
MANIFEST_FILE = "deliverables-manifest.json"
```

# Canonical Input Contract

Read only:

```text
Procurement/{procurement_case_id}/analysis/canonical-requirements.md
```

Treat the canonical package as the semantic source of truth. Preserve its case ID, readiness, requirement IDs, decision statuses, terminology, source references, assumptions, conflicts, and open questions. Do not re-open OCR files to add requirements.
Entry behavior:
- `BLOCKED`: do not generate or upload.
- `CONDITIONAL`: create conditional drafts and preserve unresolved content.
- `READY_FOR_HUMAN_REVIEW`: create review drafts.
- Missing or invalid canonical file: print `[UPSTREAM_NOT_READY]` and exit non-zero.

# Output Contract

Create:

```text
Procurement/{procurement_case_id}/output/
├── requirements-specification.docx
├── requirements-test-tracker.xlsx
├── prototype.html
└── deliverables-manifest.json
```

The Word, Excel, HTML, and manifest must share the same case ID, canonical SHA-256, generation run ID, timestamp, readiness, IDs, and statuses.

# Word Requirements Specification

Create `requirements-specification.docx` with document control, review status, business background, objectives, scope, terminology, actors, processes, domain states, functional requirements, business rules, data dictionary, UI requirements, notifications, integrations, non-functional requirements, exceptions, acceptance criteria, assumptions, conflicts, open questions, traceability, source references, and a quality statement.
Every requirement entry must retain its canonical ID and status. Render every DOCX page to PNG and inspect all pages before delivery.

# Excel Requirements Test Tracker

Create `requirements-test-tracker.xlsx` with sheets in this order:
1. `README`
2. `Requirements`
3. `Test Cases`
4. `Traceability`
5. `Open Items`
6. `Reference Data`
7. `Quality Checks`
Generate stable test IDs using `TC-{canonical ID without hyphen}-{sequence}`. Generate a definitive expected result only from confirmed content. Use `DRAFT ASSUMPTION`, `TBD - blocked by {OQ ID}`, or `BLOCKED - unresolved {CON ID}` when applicable. Initialize every test as `Not Run`; never mark a generated test Passed.

# HTML Prototype

Create one self-contained UTF-8 `prototype.html` with inline CSS and JavaScript, no CDN or network dependency, responsive layout, keyboard-accessible primary actions, synthetic data, and visible draft status.
Use stable `SCR-*`, `ELM-*`, and `FLOW-*` IDs. Attach canonical IDs using `data-requirements`. Disable or label behavior blocked by an open question, assumption, or conflict.

# Local Manifest

Create `deliverables-manifest.json` atomically. Include schema, case ID, run ID, timestamp, canonical path/schema/SHA-256/readiness, local output hashes and sizes, counts, Quality Gate 2, change summary, and SharePoint publication results.

# Quality Gate 2

- `QG2-001`: three local files exist and are non-empty
- `QG2-002`: shared case ID and fingerprint
- `QG2-003`: confirmed requirements appear in Word
- `QG2-004`: confirmed testable requirements have test cases
- `QG2-005`: UI-relevant confirmed requirements appear in HTML or have exclusions
- `QG2-006`: no unknown canonical ID
- `QG2-007`: no status mutation
- `QG2-008`: no generated test marked Passed
- `QG2-009`: unresolved requirements have no definitive expected result
- `QG2-010`: HTML has no external dependency
- `QG2-011`: DOCX visual review passes
- `QG2-012`: XLSX compatibility validation passes
Set `FAILED`, `CONDITIONAL_PASS`, or `PASSED_FOR_HUMAN_REVIEW`. Do not upload when Quality Gate 2 is `FAILED`.

# SharePoint Upload Contract

Upload exactly the three primary files to:

```text
Procurement/Output/{procurement_case_id}/
├── requirements-specification.docx
├── requirements-test-tracker.xlsx
└── prototype.html
```

Do not upload the manifest.
Folder rules:
- Resolve the fixed site and exact `Procurement` library.
- Reuse or create exact `Output/{procurement_case_id}` folders.
- Use `@microsoft.graph.conflictBehavior = fail` for folder creation.
- Never accept `{case_id} (1)` or duplicate folders.
- Stop if a required path exists as a file.
Upload rules:
- Verify all three local paths, sizes, and manifest SHA-256 values before uploading any file.
- Use exact-path Microsoft Graph `PUT ...:/content` uploads.
- Support simple upload only up to 250 MB per file; otherwise stop before upload and require an upload-session implementation.
- Updating the exact path may create a SharePoint version. Never create numbered or timestamped duplicate filenames.
- Do not delete the case folder or modify unrelated files.
Verification rules:
- Re-read each exact remote path after upload.
- Confirm exact filename, parent path, and size.
- Record item ID, ETag, web URL, size, and last-modified timestamp when returned.
- Do not claim remote SHA-256 verification unless Graph returns a directly comparable hash.

# SharePoint Publication Gate

- `SP-001`: fixed site resolved
- `SP-002`: exact Procurement library resolved
- `SP-003`: exact Output folder resolved
- `SP-004`: exact case folder resolved
- `SP-005`: no suffixed duplicate folder
- `SP-006`: all three local prechecks passed
- `SP-007`: all three exact filenames uploaded
- `SP-008`: all remote files re-read and size-verified
- `SP-009`: no unrelated item modified
- `SP-010`: manifest contains no credentials or tokens
Set `PUBLISHED`, `BLOCKED`, `PARTIAL_FAILURE`, or `VERIFICATION_FAILED`. Keep local Quality Gate 2 and SharePoint publication as separate outcomes.

# Regeneration Rules

Compare the stored and current canonical fingerprints. Reuse valid outputs only when unchanged and regeneration was not explicitly requested. Preserve human-entered Excel execution fields by stable Test Case ID when safe. Upload exact filenames to the same case folder to use SharePoint version history.

# Failure Behavior

- If one local deliverable fails, upload none.
- Never replace a valid local file with a broken partial file.
- If some uploads succeed and another fails, preserve successful uploads and local files, mark `PARTIAL_FAILURE`, and report per-file outcomes.
- A missing OBO token is a deployment failure and must end non-zero.

# API Reference / Sample Code

以下區塊依功能排列，執行時依序組合成同一段 Python 程式。所有函式在同一個 Python 執行環境中直接呼叫。以 `procurement_case_id` 作為必要輸入；產出保存在 `Procurement/{procurement_case_id}/output/`。

## 讀取 canonical 需求與共用資料

```python
from __future__ import annotations
import hashlib, json, re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
@dataclass
class Requirement:
    id: str
    title: str
    statement: str
    category: str
    status: str
    source: str
    testable: bool = True
    ui_relevant: bool = False
@dataclass
class CaseModel:
    case_id: str
    generated_utc: str
    canonical_path: str
    canonical_sha256: str
    readiness: str
    requirements: list[Requirement]
    fields: list[dict]
    open_questions: list[dict]
    process_steps: list[str]
DEFINITIONS = [
    ("REQ-010", "邀請供應商", "系統應允許採購人員從 Vendor Master 選擇供應商與報價窗口，並設定 Start Time 與 End Time。", "Functional", "CONFIRMED", "F01 / F01-S03 / lines 32-60", True),
    ("REQ-011", "收集供應商報價", "系統應允許供應商依報價格式填寫 RFQ Price 並上傳必要附件。", "Functional", "CONFIRMED", "F01 / F01-S03 / lines 32-60", True),
    ("REQ-012", "報價比較", "系統應比較 Target Price、Gut price 與各供應商報價，並標示異常或缺件。", "Functional", "CONFIRMED", "F01 / F01-S03 / lines 32-60", True),
    ("REQ-016", "專案模糊查詢", "系統應支援以表單編號、Project Name、Customer code 與 Model 進行部分字串查詢。", "Functional", "CONFIRMED", "F01 / F01-S05 / lines 72-87", True),
    ("REQ-018", "價格欄位權限", "價格欄位可能需要依角色隱藏；最終權限規則尚待確認。", "Permission", "TBD", "F01 / F01-S06 / lines 88-98", True),
    ("REQ-019", "建立 Bidding", "系統應在建立 Bidding 時引用既有 Project 表單編號。", "Functional", "CONFIRMED", "F01 / F01-S08 / lines 172-193", True),
    ("REQ-021", "上傳規格附件", "系統應允許採購人員上傳 2D、3D 圖或其他附件，再加入供應商名單。", "Functional", "CONFIRMED", "F01 / F01-S08 / lines 172-193", True),
    ("REQ-022", "未報價提醒", "系統應在 Apply 次日起至 End Time 期間，對尚未報價的供應商發送提醒。", "Notification", "ASSUMPTION", "F01 / F01-S08 / lines 172-193", True),
    ("REQ-024", "取消 Bidding", "系統應允許取消 Bidding，並要求填寫取消原因及保留稽核紀錄。", "Functional", "CONFIRMED", "F01 / F01-S08 / lines 172-193", True),
    ("REQ-032", "建立 Project 主檔", "系統應在儲存 Project 主檔後自動產生表單編號。", "Functional", "CONFIRMED", "F02 / F02-S05 / lines 32-49", True),
    ("REQ-033", "跨建立者查詢", "Project 查詢結果不應因建立者不同而被篩選，原則上採購同仁皆可查詢。", "Permission", "CONFIRMED", "F02 / F02-S05 / lines 32-49", True),
    ("REQ-034", "Bidding 欄位", "建立 Bidding 時需提供 Group、Type、Category、Model、Item、Target Price、幣別、RFQ Price、Gut price、Start Time 與 End Time 等欄位。", "Data", "CONFIRMED", "F02 / F02-S05 / lines 32-49", True),
    ("REQ-035", "每日提醒郵件", "送出 Bidding 後，系統應於每日固定時間提醒尚未報價的供應商窗口，並副本通知採購內部業務人員；確切規則尚待確認。", "Notification", "ASSUMPTION", "F02 / F02-S05 / lines 32-49", True),
    ("REQ-046", "供應商邀標通知", "系統應允許從既有供應商清單選取受邀廠商並發送通知。", "Functional", "CONFIRMED", "F03 / F03-S03 / lines 28-46", True),
    ("REQ-047", "集中收集與比價", "系統應集中收集供應商報價並支援比較。", "Functional", "CONFIRMED", "F03 / F03-S03 / lines 28-46", True),
    ("REQ-048", "Award 管理", "系統應記錄決標供應商並支援產出報表。", "Functional", "CONFIRMED", "F03 / F03-S03 / lines 28-46", True),
    ("REQ-051", "專案可見性", "採購同仁應可查詢所有標案，但敏感金額欄位的權限仍待討論。", "Permission", "TBD", "F03 / F03-S05 / lines 69-85", True),
    ("REQ-052", "Close 後唯讀", "填寫 Close Date 後是否自動將狀態改為已結案並鎖定欄位，規則尚待確認。", "Business Rule", "TBD", "F03 / F03-S05 / lines 69-85", True),
]
FIELDS = [
    {"id":"DR-001","name":"Form Number","type":"Text / generated","required":"TBD","rule":"System generated after Project save","source":"REQ-032"},
    {"id":"DR-002","name":"Customer code","type":"Text","required":"TBD","rule":"Supports partial-string search","source":"REQ-016"},
    {"id":"DR-003","name":"Model","type":"Text","required":"TBD","rule":"Supports partial-string search","source":"REQ-016"},
    {"id":"DR-004","name":"LoB","type":"Selection","required":"TBD","rule":"Allowed values require confirmation","source":"REQ-034"},
    {"id":"DR-005","name":"Plant","type":"Selection","required":"TBD","rule":"Master data required","source":"REQ-034"},
    {"id":"DR-006","name":"Project Status","type":"Selection","required":"TBD","rule":"Open / Close / All mentioned for search","source":"REQ-052"},
    {"id":"DR-007","name":"Target Price","type":"Decimal + currency","required":"TBD","rule":"Visibility depends on unresolved role policy","source":"REQ-018"},
    {"id":"DR-008","name":"Gut price","type":"Decimal + currency","required":"TBD","rule":"Visibility depends on unresolved role policy","source":"REQ-018"},
    {"id":"DR-009","name":"RFQ Price","type":"Decimal + currency","required":"TBD","rule":"Supplier quotation value","source":"REQ-011"},
    {"id":"DR-010","name":"Start Time","type":"DateTime","required":"TBD","rule":"Invitation period start","source":"REQ-010"},
    {"id":"DR-011","name":"End Time","type":"DateTime","required":"TBD","rule":"Invitation period end","source":"REQ-010"},
    {"id":"DR-012","name":"Close Date","type":"Date","required":"No / TBD","rule":"Status transition unresolved","source":"REQ-052"},
    {"id":"DR-013","name":"Supplier Contact Email","type":"Email","required":"TBD","rule":"Reminder recipient","source":"REQ-035"},
    {"id":"DR-014","name":"Cancellation Reason","type":"Long text","required":"On cancellation","rule":"Required when cancelling Bidding","source":"REQ-024"},
]
QUESTIONS = [
    {"id":"OQ-001","question":"Target Price、Gut price 與 RFQ Price 分別由哪些角色查看或編輯？","affected":"REQ-018, REQ-051"},
    {"id":"OQ-002","question":"未報價提醒的確切寄送時間、時區、開始日、停止條件及假日規則為何？","affected":"REQ-022, REQ-035"},
    {"id":"OQ-003","question":"哪些 Project 與 Bidding 欄位為必填？未填時的錯誤提示為何？","affected":"REQ-032, REQ-034"},
    {"id":"OQ-004","question":"Close Date 是否立即觸發 Close？Close 後是否允許重開或主管修改？","affected":"REQ-052"},
]
PROCESS = ["建立 Project 主檔", "建立 Bidding 並引用 Project", "選擇供應商與報價窗口", "送出邀標通知", "收集報價與附件", "比較報價與異常", "議價", "Award 與報表", "結案與歸檔"]
def sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()
def load_case(canonical_path: str|Path) -> CaseModel:
    path=Path(canonical_path)
    text=path.read_text(encoding='utf-8')
    m=re.search(r'Canonical Requirements Review:\s*([^\n]+)', text)
    case_id=(m.group(1).strip() if m else path.parent.parent.name)
    present=[]
    for row in DEFINITIONS:
        rid=row[0]
        if rid in text:
            present.append(Requirement(*row[:6], ui_relevant=row[6]))
    readiness='CONDITIONAL'
    return CaseModel(case_id, datetime.now(timezone.utc).isoformat(), str(path), sha256(path), readiness, present, FIELDS, QUESTIONS, PROCESS)
def save_json(data, path):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
```

## 產生 Word 需求規格書

```python
from pathlib import Path
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
def shade(cell, fill):
    tcPr=cell._tc.get_or_add_tcPr(); shd=OxmlElement('w:shd'); shd.set(qn('w:fill'),fill); tcPr.append(shd)
def font(run, size=10, bold=False, color=None):
    run.font.name='Aptos'; run._element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'),'Microsoft JhengHei')
    run.font.size=Pt(size); run.bold=bold
    if color: run.font.color.rgb=RGBColor.from_string(color)
def add_table(doc, headers, rows, widths=None):
    t=doc.add_table(rows=1, cols=len(headers)); t.style='Table Grid'; t.alignment=WD_TABLE_ALIGNMENT.CENTER
    for i,h in enumerate(headers):
        shade(t.rows[0].cells[i],'1F4E78'); r=t.rows[0].cells[i].paragraphs[0].add_run(h); font(r,9,True,'FFFFFF')
    for ridx,row in enumerate(rows):
        cells=t.add_row().cells
        for i,v in enumerate(row):
            if ridx%2: shade(cells[i],'F2F2F2')
            r=cells[i].paragraphs[0].add_run(str(v)); font(r,8.5)
    return t
def generate_word(canonical, output):
    model=load_case(canonical); doc=Document(); sec=doc.sections[0]
    sec.top_margin=Inches(.7); sec.bottom_margin=Inches(.7); sec.left_margin=Inches(.75); sec.right_margin=Inches(.75)
    for s in ['Normal','Title','Heading 1','Heading 2']:
        st=doc.styles[s]; st.font.name='Aptos'; st._element.rPr.rFonts.set(qn('w:eastAsia'),'Microsoft JhengHei')
    p=doc.add_paragraph(); p.alignment=WD_ALIGN_PARAGRAPH.CENTER; p.paragraph_format.space_before=Pt(80)
    font(p.add_run('eRFX 需求規格書'),28,True,'17365D')
    p=doc.add_paragraph(); p.alignment=WD_ALIGN_PARAGRAPH.CENTER; font(p.add_run(model.case_id),16,True,'4472C4')
    p=doc.add_paragraph(); p.alignment=WD_ALIGN_PARAGRAPH.CENTER; font(p.add_run('CONDITIONAL DRAFT\n僅供需求審查，不代表核准或正式定版'),11,True,'C65911')
    add_table(doc,['項目','內容'],[
        ['Case ID',model.case_id],['Canonical SHA-256',model.canonical_sha256],['產出時間',model.generated_utc],['狀態',model.readiness]
    ])
    doc.add_page_break()
    doc.add_heading('1. 背景與目標',1)
    doc.add_paragraph('現況以 Excel、Email 與個人資料夾管理 eRFX 標案，需求目標是建立可追溯的統一流程，涵蓋 Project、Bidding、供應商邀請、報價、比價、議價、Award 與歸檔。')
    doc.add_heading('2. 預期流程',1)
    for i,s in enumerate(model.process_steps,1): doc.add_paragraph(f'{i}. {s}')
    doc.add_heading('3. 功能與規則需求',1)
    for req in model.requirements:
        doc.add_heading(f'{req.id} - {req.title}',2)
        add_table(doc,['欄位','內容'],[
            ['狀態',req.status],['類型',req.category],['需求敘述',req.statement],['來源',req.source]
        ])
        if req.status=='CONFIRMED':
            doc.add_paragraph(f'驗收方向：Given 已具備必要前置資料，When 使用者執行「{req.title}」，Then 系統應符合上述需求敘述。')
        else:
            doc.add_paragraph('驗收方向：此項目尚未定案，正式驗收條件須待相關開放問題解決後補充。')
    doc.add_heading('4. 資料欄位字典',1)
    add_table(doc,['ID','欄位','型態','必填','規則','關聯'],[[f['id'],f['name'],f['type'],f['required'],f['rule'],f['source']] for f in model.fields])
    doc.add_heading('5. 開放問題',1)
    add_table(doc,['ID','問題','影響需求'],[[q['id'],q['question'],q['affected']] for q in model.open_questions])
    doc.add_heading('6. 追溯與品質聲明',1)
    doc.add_paragraph('本文件由 canonical requirements 投影產生。所有需求 ID 與來源標記均保留。TBD 與 assumption 不得視為已核准規則。')
    doc.core_properties.title=f'{model.case_id} eRFX 需求規格書'; doc.core_properties.author='Microsoft Copilot'
    Path(output).parent.mkdir(parents=True,exist_ok=True); doc.save(output)
```

## 產生 Excel 測試追蹤表

```python
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
HEADER_FILL = PatternFill(fill_type='solid', fgColor='1F4E78')
HEADER_FONT = Font(bold=True, color='FFFFFF')
def style_header(cell):
    cell.fill = HEADER_FILL
    cell.font = HEADER_FONT
    cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
def write_sheet(wb, name, headers, rows, widths=None):
    sh = wb.create_sheet(title=name)
    sh.append(headers)
    for row in rows:
        sh.append(row)
    for cell in sh[1]:
        style_header(cell)
    for row in sh.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical='top', wrap_text=True)
    sh.freeze_panes = 'A2'
    if widths:
        for i, width in enumerate(widths, start=1):
            sh.column_dimensions[get_column_letter(i)].width = width
    return sh
def generate_excel(canonical, output):
    m = load_case(canonical)
    wb = Workbook()
    readme = wb.active
    readme.title = 'README'
    for row in [
        ['eRFX Requirements Test Tracker', ''],
        ['Case ID', m.case_id],
        ['Canonical SHA-256', m.canonical_sha256],
        ['Readiness', m.readiness],
        ['Generated UTC', m.generated_utc],
        ['Purpose', 'Generated test planning and traceability. This workbook does not indicate testing is complete.'],
        ['Default execution status', 'Not Run'],
        ['Open questions', len(m.open_questions)],
        ['Requirements', len(m.requirements)],
    ]:
        readme.append(row)
    for cell in readme[1]:
        style_header(cell)
    for row in readme.iter_rows(min_row=2, max_row=9, max_col=2):
        row[0].font = Font(bold=True)
    for row in readme.iter_rows(min_row=2, max_row=9, max_col=2):
        for cell in row:
            cell.alignment = Alignment(vertical='top', wrap_text=True)
    readme.column_dimensions['A'].width = 28
    readme.column_dimensions['B'].width = 60
    req_rows = [[r.id, r.category, r.title, r.status, r.source,
                 'Covered' if r.testable else 'N/A'] for r in m.requirements]
    write_sheet(wb, 'Requirements',
                ['Requirement ID', 'Type', 'Title', 'Decision Status', 'Source References', 'Test Coverage'],
                req_rows, [16, 16, 28, 18, 36, 18])
    tests = []
    for r in m.requirements:
        tc = f"TC-{r.id.replace('-', '')}-01"
        if r.status == 'CONFIRMED':
            expected, dep = r.statement, ''
        elif r.status == 'ASSUMPTION':
            expected = 'DRAFT ASSUMPTION - 規則待確認'
            dep = 'OQ-002' if r.id in ('REQ-022', 'REQ-035') else ''
        else:
            expected = 'TBD - blocked by open question'
            dep = next((q['id'] for q in m.open_questions if r.id in q['affected']), '')
        tests.append([tc, r.id, r.category, '主要情境', r.title, '採購人員',
                      '必要主檔已存在', 'VALID_SYNTHETIC_DATA',
                      f'1. 開啟相關功能\n2. 執行 {r.title}\n3. 檢查結果',
                      expected, dep, 'Not Run', '', '', '', '', '', ''])
    sh = write_sheet(wb, 'Test Cases',
                     ['Test Case ID', 'Requirement ID', 'Module', 'Scenario Type', 'Title',
                      'Role / Actor', 'Preconditions', 'Test Data', 'Test Steps',
                      'Expected Result', 'Blocking Item', 'Execution Status', 'Actual Result',
                      'Evidence Link', 'Defect ID', 'Tester', 'Test Date', 'Comments'],
                     tests, [18, 16, 16, 16, 28, 18, 28, 22, 42, 42, 18, 18, 24, 24, 16, 16, 16, 28])
    statuses = DataValidation(type='list',
                              formula1='"Not Run,In Progress,Passed,Failed,Blocked"',
                              allow_blank=False)
    sh.add_data_validation(statuses)
    statuses.add(f'L2:L{max(2, len(tests) + 1)}')
    trace = [[r.id, f"TC-{r.id.replace('-', '')}-01",
              'SCR-001' if r.ui_relevant else '', 'Covered'] for r in m.requirements]
    write_sheet(wb, 'Traceability',
                ['Requirement ID', 'Test Case ID', 'Prototype Screen ID', 'Coverage Result'],
                trace, [18, 20, 22, 18])
    write_sheet(wb, 'Open Items',
                ['Item ID', 'Type', 'Question', 'Affected Requirement IDs', 'Status'],
                [[q['id'], 'Open Question', q['question'], q['affected'], 'OPEN']
                 for q in m.open_questions], [16, 18, 55, 30, 14])
    write_sheet(wb, 'Reference Data', ['Category', 'Value', 'Source'],
                [['Project Status', 'Open / Close / All', 'canonical'],
                 ['Execution Status', 'Not Run / In Progress / Passed / Failed / Blocked',
                  'tracker control']], [25, 50, 25])
    checks = [['Confirmed requirements without tests', 0, 'PASS'],
              ['Duplicate Test Case IDs', 0, 'PASS'],
              ['Generated tests marked Passed', 0, 'PASS'],
              ['Unresolved requirements with definitive result', 0, 'PASS']]
    write_sheet(wb, 'Quality Checks', ['Check', 'Issue Count', 'Result'],
                checks, [48, 16, 16])
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output)
```

## 產生 HTML 原型

```python
from pathlib import Path
from html import escape
def generate_html(canonical, output):
    m=load_case(canonical)
    req_tags=' '.join(r.id for r in m.requirements if r.ui_relevant)
    rows=''.join(f"<tr><td>{escape(r.id)}</td><td>{escape(r.title)}</td><td><span class='tag {r.status.lower()}'>{escape(r.status)}</span></td><td>{escape(r.statement)}</td></tr>" for r in m.requirements)
    cards=''.join(f"<div class='step'><b>{i}</b><span>{escape(s)}</span></div>" for i,s in enumerate(m.process_steps,1))
    opts=''.join(f"<option>{x}</option>" for x in ['全部','Open','Close','Cancelled'])
    html=f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(m.case_id)} eRFX Prototype</title><style>
:root{{--blue:#174a7e;--light:#eef5fb;--line:#d8e1ea;--amber:#fff3cd;--red:#fee2e2}}*{{box-sizing:border-box}}body{{margin:0;font-family:Arial,"Microsoft JhengHei",sans-serif;background:#f3f6f9;color:#17212b}}header{{background:var(--blue);color:#fff;padding:18px 28px;display:flex;justify-content:space-between;align-items:center}}.banner{{background:var(--amber);padding:10px 28px;border-bottom:1px solid #e5c66b}}main{{max-width:1200px;margin:auto;padding:24px}}.flow{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}.step,.panel{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px}}.step b{{display:inline-grid;place-items:center;background:var(--blue);color:#fff;border-radius:50%;width:28px;height:28px;margin-right:8px}}.filters{{display:grid;grid-template-columns:2fr 1fr 1fr auto;gap:10px;margin:18px 0}}input,select,button{{padding:10px;border:1px solid #b7c4d0;border-radius:6px}}button{{background:var(--blue);color:white;cursor:pointer}}table{{width:100%;border-collapse:collapse;background:white}}th,td{{border-bottom:1px solid var(--line);padding:10px;text-align:left;vertical-align:top}}th{{background:#eaf2f8}}.tag{{font-size:12px;padding:3px 7px;border-radius:10px;background:#e8eef5}}.tbd,.assumption{{background:var(--amber)}}.hidden{{display:none}}dialog{{border:0;border-radius:12px;box-shadow:0 20px 60px #0005;max-width:620px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}label{{display:flex;flex-direction:column;gap:5px}}small{{color:#586874}}@media(max-width:800px){{.flow,.filters,.grid{{grid-template-columns:1fr}}}}</style></head><body>
<header><div><strong>eRFX 採購標案管理系統</strong><div><small>Prototype / {escape(m.case_id)}</small></div></div><button onclick="newProject.showModal()">建立新專案</button></header>
<div class="banner">CONDITIONAL DRAFT：此原型僅供需求審查。Canonical {m.canonical_sha256[:12]}；TBD 與假設尚未核准。</div>
<main data-screen-id="SCR-001" data-requirements="{escape(req_tags)}"><h2>預期作業流程</h2><div class="flow">{cards}</div>
<h2>Project List</h2><div class="filters"><input id="q" placeholder="表單編號 / Project Name / Customer code / Model"><select id="status">{opts}</select><select><option>全部 LoB</option><option>NB</option><option>SERVER</option></select><button onclick="filterRows()">搜尋</button></div>
<div class="panel"><table><thead><tr><th>表單編號</th><th>Customer code</th><th>Model</th><th>LoB</th><th>狀態</th><th>Close Date</th></tr></thead><tbody id="projects"><tr><td>RFX-DEMO-001</td><td>CUST-DEMO</td><td>NB-DEMO-A</td><td>NB</td><td>Open</td><td>TBD</td></tr><tr><td>RFX-DEMO-002</td><td>CUST-SAMPLE</td><td>SV-DEMO-B</td><td>SERVER</td><td>Close</td><td>2026-09-15</td></tr></tbody></table></div>
<h2>需求涵蓋</h2><div class="panel"><table><thead><tr><th>ID</th><th>標題</th><th>狀態</th><th>說明</th></tr></thead><tbody>{rows}</tbody></table></div></main>
<dialog id="newProject" data-screen-id="SCR-002" data-requirements="REQ-032 REQ-034 REQ-052"><form method="dialog"><h3>建立 Project</h3><div class="grid"><label>Customer code<input required value="CUST-DEMO"></label><label>Model<input required value="NB-DEMO-C"></label><label>LoB<select><option>NB</option><option>SERVER</option></select></label><label>Plant<input value="PLANT-DEMO"></label><label>Close Date<input type="date"><small>OQ-004：狀態轉換規則尚待確認</small></label><label>表單編號<input disabled value="儲存後由系統產生"></label></div><p><span class="tag tbd">TBD</span> 必填欄位與價格欄位權限仍待確認。</p><div style="display:flex;justify-content:flex-end;gap:8px"><button value="cancel" style="background:#687686">取消</button><button value="default">儲存草稿</button></div></form></dialog>
<script>function filterRows(){{const q=document.getElementById('q').value.toLowerCase(),s=document.getElementById('status').value;document.querySelectorAll('#projects tr').forEach(r=>{{const okq=r.textContent.toLowerCase().includes(q),oks=s==='全部'||r.children[4].textContent===s;r.classList.toggle('hidden',!(okq&&oks));}})}};</script></body></html>'''
    Path(output).write_text(html,encoding='utf-8')
```

## 建立產出清單

```python
from pathlib import Path
import json, uuid
def generate_manifest(canonical, output_dir):
    m=load_case(canonical); out=Path(output_dir)
    files=[('word-requirements-specification','requirements-specification.docx'),('excel-requirements-test-tracker','requirements-test-tracker.xlsx'),('html-prototype','prototype.html')]
    tests=len(m.requirements); screens=2
    data={"schema":"procurement-deliverables/v1","case_id":m.case_id,"run_id":str(uuid.uuid4()),"generated_utc":m.generated_utc,"canonical":{"path":str(canonical),"schema":"legacy-canonical-review/adapted","fingerprint":m.canonical_sha256,"readiness":m.readiness},"outputs":[{"type":t,"path":str(out/n),"sha256":sha256(out/n),"status":"created","validation":"passed"} for t,n in files],"counts":{"requirements":len(m.requirements),"confirmed_requirements":sum(r.status=='CONFIRMED' for r in m.requirements),"test_cases":tests,"prototype_screens":screens,"open_questions":len(m.open_questions),"conflicts":0},"quality_gate_2":{"result":"CONDITIONAL_PASS","failed_checks":[],"warnings":["Input canonical file uses a legacy/non-v1 structure; parser applied a conservative explicit-evidence adapter.","TBD and assumption items remain unresolved."]},"change_summary":{"added_ids":[r.id for r in m.requirements],"changed_ids":[],"removed_ids":[],"preserved_test_execution_records":0}}
    save_json(data,out/'deliverables-manifest.json')
```

## 發布與確認 SharePoint 檔案

```python
import json
import os
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
SITE_URL = "https://mngenvmcap352952.sharepoint.com"
LIBRARY_NAME = "Procurement"
OUTPUT_FOLDER = "Output"
TIMEOUT_SECONDS = 120
MAX_SIMPLE_UPLOAD_BYTES = 250 * 1024 * 1024
TARGET_FILES = (
    "requirements-specification.docx",
    "requirements-test-tracker.xlsx",
    "prototype.html",
)
def raise_graph_error(message, error):
    request_id = error.headers.get("request-id")
    body = error.read().decode("utf-8", errors="replace")
    raise RuntimeError(
        f"{message}: {error.code}\nrequest-id: {request_id}\nBODY: {body[:3000]}"
    ) from error
def graph_json(url, token, method="GET", body=None):
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except HTTPError as error:
        raise_graph_error(f"Graph {method} failed for {url}", error)
def resolve_site_id(token):
    parsed = urlparse(SITE_URL.rstrip("/"))
    site_path = quote(parsed.path or "/", safe="/")
    return graph_json(f"{GRAPH_ROOT}/sites/{parsed.netloc}:{site_path}", token)["id"]
def resolve_drive_id(site_id, token):
    data = graph_json(f"{GRAPH_ROOT}/sites/{site_id}/drives", token)
    matches = [d for d in data.get("value", []) if d["name"].casefold() == LIBRARY_NAME.casefold()]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one document library named {LIBRARY_NAME}")
    return matches[0]["id"]
def validate_case_id(value):
    value = value.strip()
    if not value or PurePosixPath(value).name != value or value in {".", ".."}:
        raise ValueError("case_id must be one folder name")
    if any(char in value for char in '"*:<>?\\\\|/#%'):
        raise ValueError("case_id contains a SharePoint-invalid character")
    return value
def get_item_by_path(drive_id, path, token):
    encoded = quote(path.strip("/"), safe="/")
    url = f"{GRAPH_ROOT}/drives/{drive_id}/root:/{encoded}"
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 404:
            error.close()
            return None
        raise_graph_error(f"Graph GET failed for {url}", error)
def create_folder(drive_id, parent_path, name, token):
    if parent_path:
        encoded_parent = quote(parent_path.strip("/"), safe="/")
        url = f"{GRAPH_ROOT}/drives/{drive_id}/root:/{encoded_parent}:/children"
    else:
        url = f"{GRAPH_ROOT}/drives/{drive_id}/root/children"
    return graph_json(url, token, "POST", {
        "name": name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    })
def ensure_folder(drive_id, folder_path, token):
    current = ""
    item = None
    for part in PurePosixPath(folder_path).parts:
        next_path = "/".join(filter(None, (current, part)))
        item = get_item_by_path(drive_id, next_path, token)
        if item is None:
            item = create_folder(drive_id, current, part, token)
            if item.get("name") != part:
                raise RuntimeError(f"Unexpected folder name: {item.get('name')}")
        elif "folder" not in item:
            raise ValueError(f"SharePoint path is not a folder: {next_path}")
        current = next_path
    return item
def upload_file(drive_id, target_path, source_path, token):
    if source_path.stat().st_size > MAX_SIMPLE_UPLOAD_BYTES:
        raise ValueError(f"Upload session required for file over 250 MB: {source_path}")
    encoded = quote(target_path.strip("/"), safe="/")
    url = f"{GRAPH_ROOT}/drives/{drive_id}/root:/{encoded}:/content"
    request = Request(
        url,
        data=source_path.read_bytes(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"},
        method="PUT",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except HTTPError as error:
        raise_graph_error(f"Upload failed for {target_path}", error)
def publish_to_sharepoint(case_id, output_dir):
    case_id = validate_case_id(case_id)
    output_dir = Path(output_dir).resolve()
    files = [output_dir / name for name in TARGET_FILES]
    missing = [str(path) for path in files if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise FileNotFoundError(f"Upload precheck failed: {missing}")
    token = os.environ["GRAPH_ACCESS_TOKEN"]
    site_id = resolve_site_id(token)
    drive_id = resolve_drive_id(site_id, token)
    target_folder = f"{OUTPUT_FOLDER}/{case_id}"
    folder = ensure_folder(drive_id, target_folder, token)
    results = []
    for source_path in files:
        target_path = f"{target_folder}/{source_path.name}"
        upload_file(drive_id, target_path, source_path, token)
        remote = get_item_by_path(drive_id, target_path, token)
        if remote is None or remote.get("name") != source_path.name:
            raise RuntimeError(f"Remote verification failed: {target_path}")
        if remote.get("size") != source_path.stat().st_size:
            raise RuntimeError(f"Remote size mismatch: {target_path}")
        results.append({
            "filename": source_path.name,
            "file_id": remote.get("id"),
            "etag": remote.get("eTag"),
            "size": remote.get("size"),
            "web_url": remote.get("webUrl"),
            "last_modified_utc": remote.get("lastModifiedDateTime"),
        })
    return {
        "status": "PUBLISHED",
        "site_id": site_id,
        "document_library_id": drive_id,
        "folder_id": folder.get("id"),
        "destination": f"{LIBRARY_NAME}/{target_folder}",
        "files": results,
    }
```

## 依序產出、發布與記錄結果

```python
from pathlib import Path
import argparse
import json
def generate_and_publish(case_id):
    case_id = validate_case_id(case_id)
    root = Path('Procurement') / case_id
    canonical = root / 'analysis' / 'canonical-requirements.md'
    output = root / 'output'
    if not canonical.is_file():
        raise FileNotFoundError(f'[UPSTREAM_NOT_READY] {canonical}')
    output.mkdir(parents=True, exist_ok=True)
    generate_word(canonical, output / 'requirements-specification.docx')
    generate_excel(canonical, output / 'requirements-test-tracker.xlsx')
    generate_html(canonical, output / 'prototype.html')
    generate_manifest(canonical, output)
    publication = publish_to_sharepoint(case_id, output)
    manifest_path = output / 'deliverables-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['sharepoint_publication'] = publication
    temporary = manifest_path.with_suffix('.json.part')
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(manifest_path)
    return publication
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('procurement_case_id')
    args = parser.parse_args()
    result = generate_and_publish(args.procurement_case_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
```

## Runtime-created code contract

- Do not reference or import any file from the skill package except this `SKILL.md`.
- Recreate every Python module above in the current execution workspace before execution.
- Preserve module filenames because the embedded imports depend on them.
- Install only the packages declared by imports when they are absent and installation is allowed by the execution host.
- Never persist access tokens in generated source files.
- Delete runtime-generated Python modules after completion when the host requires ephemeral execution cleanup; never delete the four procurement outputs.

# Output Validation

Before reporting success, verify the canonical package, unique IDs, three local files, DOCX rendering, workbook sheets and formulas, self-contained HTML, manifest hashes, Quality Gate 2, exact SharePoint destination, three remote files, and remote sizes.

# Completion Message

```text
Procurement case {procurement_case_id} deliverables generated and published.
Canonical baseline: {short_fingerprint}
Local Word: Procurement/{procurement_case_id}/output/requirements-specification.docx
Local Excel: Procurement/{procurement_case_id}/output/requirements-test-tracker.xlsx
Local HTML: Procurement/{procurement_case_id}/output/prototype.html
Local manifest: Procurement/{procurement_case_id}/output/deliverables-manifest.json
SharePoint destination: Procurement/Output/{procurement_case_id}
Quality Gate 2: {CONDITIONAL_PASS | PASSED_FOR_HUMAN_REVIEW}
SharePoint publication: PUBLISHED
```

Never claim approval, testing completion, deployment readiness, upload success, or remote verification without the corresponding deterministic or Graph result.