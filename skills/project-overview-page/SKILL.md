---
name: project-overview-page
description: 產生「技術專案總覽單頁 HTML」。當使用者要求製作專案總覽、架構說明頁、產品/方案 one-pager、workshop 說明頁，或說「用總覽頁風格」「做一頁 HTML 說明」時使用。輸出單一自給自足（inline CSS + inline SVG、無外部相依）的 HTML 檔。
---

# Project Overview Page（專案總覽單頁）

產生一份**單檔、無外部相依**的 HTML 技術總覽頁：白底、細灰線、藍色重點、卡片式排版，適合工程文件、方案說明、workshop 教材索引。

## 使用時機

- 專案 / 方案總覽（overview、one-pager）
- 架構說明（含流程圖、Container Apps / 服務拓撲）
- 功能盤點、Notebook / 模組導讀
- 內部技術提案、交付說明

不適用：需要互動、即時資料、表單的頁面（那屬於 app），或長篇敘事文件（那屬於 Word）。

## 硬性規則

1. **單一 HTML 檔**：CSS 寫在 `<style>`、圖用 inline `<svg>`。禁止 CDN、外部字型、外部圖片、JavaScript。
2. **設計 token 一律用 CSS 變數**（見透過 `read_skill_resource` 取得的 `style.css`，取得方式見下方「前置步驟」），不要在元素上硬寫顏色。**不得新增、刪除、更名任何 CSS 變數**；`style.css` 沒有定義的變數（例如把 `--accent-soft` 簡化成 `--soft`）一律禁止。
3. **不要自創配色**：只用 accent 藍、green、amber 三種語意色 + 灰階。
4. **不要自創 class 或排版系統**：只能使用 `style.css` / `template.html`（皆透過 `read_skill_resource` 取得）中已定義的 class（如 `.cap`、`.nb`、`.flow`）。禁止另外發明如 `.grid`、`.grid-2`、`.grid-3` 這類本檔案未定義的 utility class；需要多欄排版時，改用既有 `.cap ul`（`grid-template-columns: repeat(auto-fit, minmax(250px,1fr))`）或 `.nb`（`grid-template-columns: repeat(auto-fit, minmax(280px,1fr))`）的既有寫法。
5. **不編造內容**：資料不足時放明顯佔位（如 `[待補：吞吐量數據]`），不得杜撰數字、名稱、URL。
6. **語言跟隨使用者**；中文頁面 `<html lang="zh-Hant">`，字型堆疊須含 `"Noto Sans TC", "Microsoft JhengHei"`。
7. **技術名詞用 `<code>`**：參數、檔名、路徑、endpoint、token 一律 code 化。

## 【前置步驟｜必做】呼叫 `read_skill_resource` 取得 Reference File 內容

**這是一個 tool call，發生在你（LLM）決定寫任何 Python 程式碼之前，不是要在產生出來的程式碼裡用 `open()`/`os.path` 對檔案系統做讀取。** Skill 資產（`assets/` 底下的檔案）可能是靜態掛載，也可能是動態儲存在 blob（不在任何 sandbox 檔案系統路徑上），`read_skill_resource` 這個 tool 負責把兩種情況統一抽象成同一個介面——你只要呼叫它，不用、也不該自己猜測或組出檔案路徑。

### 0. 呼叫方式

```
read_skill_resource(skill_name="project-overview-page", resource_name="style.css")
read_skill_resource(skill_name="project-overview-page", resource_name="template.html")
```

- `skill_name` 固定為本 skill 的名稱：`project-overview-page`（即本文件開頭 frontmatter 的 `name` 欄位）。
- `resource_name` 為 case-insensitive 查找 key，此處為 `style.css` 與 `template.html`。
- **禁止**在 `resource_name` 前面加上 `assets/` 或任何路徑前綴——這是資源查找用的 key，不是檔案路徑。

> ⚠️ 若上述 `resource_name` 實際查無結果，代表 skill 資產的註冊 key 命名方式與本文件假設不同（可能含子目錄前綴、或用了不同的檔名大小寫），此時不要自行臆測改用檔案系統路徑繞過，而是照下方「失敗時」段落回報，交由平台方（skill 維護者）核對 `skills_provider_factory.py` 的實際資源註冊邏輯。

### 1. 讀取結果的處理

`read_skill_resource` 失敗時回傳的是**一個以 `"Error: "`開頭的字串**，而不是拋出例外（exception）。因此：

- **不要**用 `try/except` 去判斷是否讀取成功——呼叫本身幾乎不會拋錯，你必須**檢查回傳字串的內容**是否以 `Error:` 開頭。
- 只有在回傳內容明確不是以 `Error:` 開頭時，才視為讀取成功，可以繼續使用。

取得的內容，是唯一的樣式與結構真相來源。**禁止**僅憑本 `SKILL.md` 的文字敘述（如「版面骨架」「元件速查」段落）去重建或推測這兩份資源的實際內容——那些段落只是給人看的摘要，不等於實作細節（例如 CSS 變數的完整清單、SVG `<marker>` 的精確座標、`.cap ul` 用的是 grid 還是 columns），實作細節只存在於透過 `read_skill_resource` 取得的原文本身。

取得後，**逐字**（不改寫、不精簡、不改變數名、不換標籤）以 Python 字串字面值（建議用三引號 raw string）內嵌進你即將產生的程式碼中，例如：

```python
STYLE_CSS = r'''<read_skill_resource 回傳的 style.css 完整內容>'''
TEMPLATE_HTML = r'''<read_skill_resource 回傳的 template.html 完整內容，供之後用 regex 抽取 marker/footer 等片段>'''
```

最終產生的 Python 程式碼**不得**包含任何對檔案系統的 skill 資產讀取邏輯（`open()`、`os.path.join("skills", ...)`、`os.listdir("skills")` 等一律禁止）——這些手法在 skill 資產為動態載入（Mode B）時必然無效，因為 sandbox 檔案系統上根本不存在對應路徑。

### 2. 若讀取失敗

若任一資源的回傳內容以 `Error:` 開頭，**必須在回覆最開頭明確聲明**：

> ⚠️ 呼叫 `read_skill_resource(skill_name="project-overview-page", resource_name="xxx")` 失敗，回傳：[完整錯誤字串]。以下內容為降級處理，未套用完整參考樣式。

不得在讀取失敗的情況下，安靜地生出一份「風格相似但實作不同」的頁面並宣稱完成。

## 版面骨架（依序）

```
.wrap (max-width 1040px, padding 56px 24px 72px)
├─ header.hero        眉標 + H1 + 導言（lede）
├─ section 01         運作原理  → .flow 步驟鏈 + .callout
├─ section 02         功能總覽  → .cap 分組卡（A/B/C）
├─ section 03         模組導讀  → .nb 卡片格 + .tag 標籤
├─ section 04         架構圖    → .arch inline SVG + .legend + .callout
└─ footer             交付內容 / 檔案位置
```

章節標題固定格式：`<h2><span class="num">01</span>標題</h2>`，下方接一行 `<p class="section-sub">` 補充語。編號兩位數、由 01 起連續。

## 元件速查

| 元件 | 類別 | 用途 |
|---|---|---|
| 眉標膠囊 | `.eyebrow` | Hero 上方一行定位標語（如「OpenAI 相容 · 本地優先」） |
| 導言 | `.lede` | H1 下方 2–3 句摘要，最寬 720px，關鍵句用 `<strong>` |
| 流程鏈 | `.flow` > `.step`（`.k` 序號 / `.t` 標題 / `.d` 說明）+ `.arrow` | 3–5 步的線性流程，步驟間插 `<div class="arrow">→</div>` |
| 能力分組 | `.cap` > `.cap-head`（`.n` 字母標 + `h3` + `.sum`）+ `ul` | 每組 3–6 條，條列自動雙欄 |
| 模組卡 | `.nb` > `.nbcard`（`.file` 檔名 / `h3` / `.one` 一句話 / `ul` / `.tags`） | 檔案或模組導讀，卡片高度自動對齊、標籤貼底 |
| 標籤 | `.tag.free`（綠）/ `.tag.cost`（琥珀）/ `.tag.neutral`（灰） | 語意固定：free = 免費/免金鑰、cost = 會產生費用/需金鑰、neutral = 前置條件 |
| 架構圖 | `.arch` > `<svg>` + `.legend` | 見下方 SVG 規範 |
| 重點框 | `.callout` | 每節最多一個，放安全性質、限制、注意事項；開頭用 `<strong>標題：</strong>` |

## SVG 架構圖規範

- `viewBox="0 0 960 430"`，`role="img"` + `aria-label` 描述。
- 節點：`<rect rx="12">`，`fill="#ffffff"`、`stroke="#e5e7eb"`；標題 `font-size="14" font-weight="650" fill="#1f2937"`，副標 `font-size="12" fill="#6b7280"`，皆 `text-anchor="middle"`。
- 邊界群組（環境 / VNet / RG）：`rect` `fill="#f8fafc"`、`stroke="#cbd5e1"`、`stroke-dasharray="6 5"`，上緣放 12.5px 灰色標題。
- 連線：主要資料流用實線 `#2563eb` + `marker-end="url(#ah)"`；次要／控制流用虛線 `#9ca3af` + `url(#ahg)`。兩個 `marker` 定義**必須逐字複製自**透過 `read_skill_resource` 取得的 `template.html`（含 `refX`、`refY`、`markerUnits`、`path d` 座標），**不得自行重新繪製箭頭**。建議直接用 regex 從 `TEMPLATE_HTML` 字串抽取 `<marker id="ah">...</marker>` / `<marker id="ahg">...</marker>` 區塊後原樣複用，而不是手動重新輸入座標。
- 圖下必附 `.legend`，用 `.ln` / `.ln.d` 說明實線與虛線各代表什麼。
- 圖需水平捲動（`min-width: 720px`），不要為了塞進畫面而縮字。

## 寫作風格

- 導言、`.section-sub`、`.one` 用完整句；條列不加句號。
- 一條列 = 一個事實，控制在一行內（約 30–45 字）。
- 用具體值取代形容詞：寫 `docker compose 一鍵啟動兩個容器`，不寫「部署非常簡便」。
- 因果用破折號或「——」收尾強調結論，關鍵結論加 `<strong>`。

## 產出步驟

1. **呼叫 `read_skill_resource`** 取得 `style.css`、`template.html` 全文（見上方「前置步驟」），並確認回傳內容不是以 `Error:` 開頭。
2. 盤點內容：定位標語、一句話價值、流程步驟、能力分組、模組清單、架構節點與連線、交付說明。
3. 將取得的 `style.css` 內容**逐字元、不精簡、不改寫變數名**貼進 `<style>`（建議以字串字面值方式直接嵌入，見前置步驟範例）。
4. 依取得的 `template.html` 的 DOM 結構與 class 用法填入章節內容；缺的章節整段拿掉，不要留空殼；不得自創替代結構（例如把 grid 排版改成 CSS `columns`、把徽章改成方塊）。
5. **自檢（產出前逐項確認，任一項不通過就必須修正後才可輸出檔案）**：
   - [ ] 無外部資源、無 JS
   - [ ] 所有 code 名詞已 `<code>` 化
   - [ ] 標籤（`.tag.free/.cost/.neutral`）語意正確
   - [ ] SVG 有 `.legend`，章節編號連續
   - [ ] 列出輸出 HTML 中所有 `var(--xxx)`，逐一確認每個變數名稱都存在於取得的 `style.css`（不可有 `--soft` 這類自創替代變數）
   - [ ] 列出輸出 HTML 中所有 class 名稱，逐一確認都存在於取得的 `style.css` 或 `template.html`（不可有 `.grid` / `.grid-2` 這類自創 class）
   - [ ] 沒有任何顏色是直接寫死 hex（除非該 hex 本身就是複製自取得的 `style.css` 原文，如 tag 的邊框色）
   - [ ] SVG `<marker id="ah">` / `id="ahg"` 的 `refX`／`refY`／`path d` 與取得的 `template.html` 完全一致
   - [ ] `<style>` 內含有 `--eaa-ref-fidelity-check` 變數，且值與取得的 `style.css` 原文完全一致（此變數為「是否忠實複製 style.css」的驗證水印，不影響視覺呈現，禁止刪除或改值）
   - [ ] 產生的程式碼本身不含 `open()`、`os.path.join("skills", ...)`、`os.listdir("skills")` 等對檔案系統的 skill 資產讀取邏輯
6. 存成 `<主題>-overview.html`，並確認檔案確實存在後才回報完成。回報時附上上一步自檢清單的結果，以及 `read_skill_resource` 兩次呼叫是否成功。

## 參考檔

- `style.css`（透過 `read_skill_resource(skill_name="project-overview-page", resource_name="style.css")` 取得）— 完整設計 token 與元件樣式（直接內嵌，逐字複製，禁止改寫）
- `template.html`（透過 `read_skill_resource(skill_name="project-overview-page", resource_name="template.html")` 取得）— 可直接改寫的骨架範例（DOM 結構、SVG marker 定義均為權威來源）