-- ============================================================
-- EAA Skill RBAC — v2.1 → v2.2 Migration (DDL only)
-- ============================================================
-- 2026.08.21 George : Skill Registry 分階(scenario / internal 兩層 skill)
--
-- 適用對象:已經跑過 `skill_rbac_schema v2.sql`(+ v2.1 migration)的既有部署。
--   全新部署(Greenfield)不需要跑這支——直接執行 v2.sql 即可,該檔已含
--   is_internal 定義。
--
-- 這個欄位在解什麼問題:
--   跨 MCP 的編排知識(例如「送假單前必須先向 Work IQ Calendar 取當日
--   occurrences」)原本只能寄生在 child skill 的 frontmatter,對所有情境
--   常駐、彼此稀釋 salience。解法是引入「情境層 parent skill」:
--     leave-workflow (scenario)  ← 只有這層對外可見
--         └── hr-leave-system (internal)  ← 對外隱藏,由 parent 正文指名載入
--   is_internal 就是「這個 skill 不再獨立可被發現」的那個標記。
--
-- ⚠️ 這個欄位「不」進 v_my_skills 的 WHERE —— 這是本次設計最關鍵的一點:
--   v_my_skills 是 MCP 對外 list_skills 與 MAF runtime materialize
--   (skills_provider_factory.create_provider_scope())共用的唯一查詢。
--   在 SQL 端過濾會讓 internal skill 連 runtime 都載不進來,得到一個
--   「parent 叫我去拿、但拿不到」的死路。
--   → 過濾只發生在應用層的投影(core_handler.list_skills()),
--     view 只負責「把欄位交出去」。
--
-- ⚠️ 同理,絕對不要塞進 RLS predicate:
--   RLS 回答「誰能用」(身分),is_internal 回答「什麼時候該被看見」(拓樸)。
--   混進 predicate 會讓它變成安全邊界的一部分,日後每次 review 都要重新
--   論證它到底在擋什麼。本檔完全不動 UserSkillGrantsPolicy /
--   fn_user_skill_grants_predicate。
--
-- 為什麼這支很安全:
--   - 純新增欄位 + DEFAULT 0 → 既有 skill 全部維持現狀,不會有任何一個
--     從清單消失
--   - v_my_skills 的 WHERE 一個字不動 → 既有可見性規則零影響
--   - 唯一 SCHEMABINDING 的 fn_user_skill_grants_predicate 綁在
--     user_skill_grants 上,不是 skills
--   → 不需要拆 SECURITY POLICY、不需要 DROP/CREATE 表
--
-- 執行順序:
--   1. 用 admin 身份連 Azure SQL / Fabric SQL,執行本檔
--   2. 跑第 3 節的驗證查詢
--   3. 同步更新應用端:skills_provider_factory._fetch_allowed_skills() 的
--      explicit SELECT 清單必須加上 is_internal,否則新欄位查不到
-- ============================================================


-- ============================================================
-- 1. skills 表新增 is_internal
-- ============================================================
-- 為什麼是 BIT 而不是 VARCHAR enum('scenario','internal'):
--   - 這張表既有的旗標全是 BIT(is_public / enabled),VARCHAR enum 會是
--     唯一的異類
--   - BIT 天生二值,不需要額外的 CHECK constraint 要維護
--   - pyodbc 直接映射成 Python bool,應用端不需轉型
--
-- DEFAULT 0 是刻意的:現有 skill 全部維持現狀,只有明確標記的才消失。
-- 此 migration 不會弄壞任何既有場景。
--
-- ⚠️ is_internal 與 is_public 是「正交」的兩個維度,不是反義詞:
--   - is_public   → 身分授權:要不要逐一 grant 才能用
--   - is_internal → 目錄拓樸:該不該出現在對外的 skill 目錄裡
--   兩者可以同時為 1(所有人都有權使用,但只能透過 parent 被叫到)。
--   維護時請不要把這兩個欄位當成一組來理解。

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.skills') AND name = 'is_internal'
)
BEGIN
    ALTER TABLE dbo.skills ADD is_internal BIT NOT NULL
        CONSTRAINT DF_skills_is_internal DEFAULT 0;

    PRINT N'[v2.2] dbo.skills.is_internal 已新增(預設 0 = 對外可見)。';
END
ELSE
BEGIN
    PRINT N'[v2.2] dbo.skills.is_internal 已存在,略過。';
END
GO


-- ============================================================
-- 2. v_my_skills 加開 is_internal
-- ============================================================
-- 讀路徑(skills_provider_factory._fetch_allowed_skills())是自列欄位的 explicit
-- SELECT + 欄位名稱取值,本 view 的欄位順序不影響它;但「新增/更名欄位」時
-- 仍需同步該處的 SELECT 清單,否則新欄位查不到。
--
-- 再次強調:WHERE 子句與 v2.1 完全相同,一個字都沒改。

CREATE OR ALTER VIEW [dbo].[v_my_skills] AS
SELECT
    s.skill_name,
    s.owner_upn,
    s.is_public,
    CASE WHEN s.owner_upn IS NULL THEN 'global' ELSE 'private' END AS skill_scope,
    s.skill_key,
    s.blob_path,
    s.blob_prefix,
    s.is_internal,
    s.updated_at
FROM skills s
LEFT JOIN user_skill_grants g ON g.skill_key = s.skill_key
WHERE s.enabled = 1
  AND (
        (g.user_upn IS NOT NULL AND (g.expires_at IS NULL OR g.expires_at > SYSUTCDATETIME()))
     OR s.is_public = 1
      );
GO


-- ============================================================
-- 3. 驗證
-- ============================================================
-- 預期:
--   - 所有既有 skill 的 is_internal 都是 0
--   - v_my_skills 查得到 is_internal 欄位
--   - RLS 過濾行為與升級前完全一致

-- 3.1 目視檢查 — 全部應為 0
SELECT skill_name, owner_upn, skill_key, is_public, is_internal
FROM dbo.skills
ORDER BY skill_name;
GO

-- 3.2 遷移後斷言 — 應回傳 0 列。回傳任何列代表有 skill 被意外標記。
SELECT skill_name, is_internal
FROM dbo.skills
WHERE is_internal <> 0;
GO

-- 3.3 以一般使用者身份連線跑,確認 RLS 過濾未受影響且新欄位有值
-- SELECT skill_name, is_internal FROM dbo.v_my_skills;
-- GO


-- ============================================================
-- 4. 標記 / 還原 internal skill(操作範例,預設不執行)
-- ============================================================
-- ⚠️ 改完之後不會立刻生效:
--   應用端的 skill 清單快取是 SkillsProviderFactory._rls_cache
--   (key = sha256(sql_token),TTL = SKILLS_RLS_CACHE_TTL,預設 300 秒)。
--   這個快取「不是」用 updated_at 當 key —— 用 updated_at 當 key 的是
--   skill 內容快取(_content_cache / _listing_cache)。
--   所以「bump updated_at 讓 visibility 生效」是無效的做法,別這樣做。
--   要立刻生效:呼叫 SkillsProviderFactory.invalidate_rls_cache(),
--   或直接等 TTL 過期(≤ 300 秒),或建立新的 ACA revision。

-- 標為 internal(從對外目錄隱藏,但 runtime 仍可被 parent 指名載入):
-- UPDATE dbo.skills SET is_internal = 1 WHERE skill_key = 'hr-leave-system';
-- GO

-- 還原為對外可見:
-- UPDATE dbo.skills SET is_internal = 0 WHERE skill_key = 'hr-leave-system';
-- GO

-- 4.1 孤兒偵測提醒
-- 「is_internal = 1 但沒有任何 parent 的 metadata.children 指向它」= 永久
-- 不可被發現、也不會有人叫它。這個對帳「無法用純 SQL 完成」,因為
-- metadata.children 存在 Blob 的 SKILL.md frontmatter 裡,SQL 看不到。
-- 對帳實作在應用層:
--   - core_handler.list_skills() 會在 admin bypass 路徑印 WARNING
--   - tests/test_skill_topology.py 在 CI 擋
-- 這裡只列出「候選清單」供人工交叉比對:
-- SELECT skill_name FROM dbo.skills WHERE is_internal = 1 AND enabled = 1;
-- GO
