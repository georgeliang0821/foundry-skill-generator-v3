-- ============================================================
-- EAA Skill RBAC — v2 → v2.1 Migration (DDL only)
-- ============================================================
-- 2026.08.14 George : Skill Folder 格式支援(SKILL.md + references/ + assets/)
--
-- 適用對象:已經跑過 `skill_rbac_schema v2.sql` 的既有部署。
--   全新部署(Greenfield)不需要跑這支——直接執行 v2.sql 即可,該檔已含
--   blob_prefix 定義。
--
-- 本檔只做 DDL,不含任何 data migration:
--   - blob_prefix 是 computed column,SQL Server 會依既有列的 skill_name /
--     owner_upn 自動推導,不需要 UPDATE 既有資料。
--   - Blob Storage 上的實體檔案不動。既有 skill 只有 SKILL.md 一個檔時,
--     以 blob_prefix 列舉的結果就是那一個檔,行為與升級前完全一致。
--
-- 為什麼這支很安全:
--   - 純新增欄位,blob_path 定義完全不動 → 既有讀路徑零影響
--   - blob_prefix 上沒有任何 index,PK 在 skill_key
--   - v_my_skills 是 CREATE OR ALTER 且「非」SCHEMABINDING
--   - 唯一 SCHEMABINDING 的 fn_user_skill_grants_predicate 綁在
--     user_skill_grants 上,不是 skills
--   → 因此不需要拆 SECURITY POLICY、不需要 DROP/CREATE 表
--
-- 注意:ADD ... PERSISTED 會對既有列做一次全表計算並落地。skills 表的
--   資料量級(數十至數百列)下瞬間完成;若貴環境列數異常大,請安排維護窗口。
--
-- 執行順序:
--   1. 用 admin 身份連 Azure SQL / Fabric SQL,執行本檔
--   2. 跑第 3 節的驗證查詢確認 blob_prefix 推導正確
-- ============================================================


-- ============================================================
-- 1. skills 表新增 blob_prefix computed column
-- ============================================================
-- 公式 = blob_path 去掉結尾的 'SKILL.md'。結尾的 '/' 是必要的:
-- 少了它,list_blobs(name_starts_with='skills/foo') 會連 'skills/foobar/'
-- 底下的檔案一起撈進來,等同跨 skill 汙染。
--
-- 應用端必須以相同公式自行推導(見 skills_sync.skill_blob_prefix())——
-- 寫路徑在上傳 Blob 的那一刻還沒有列可讀。任何一邊改公式,另一邊必須同步。

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.skills') AND name = 'blob_prefix'
)
BEGIN
    ALTER TABLE dbo.skills ADD blob_prefix AS (
        CASE
            WHEN owner_upn IS NULL
                THEN CAST('skills/' + skill_name + '/' AS VARCHAR(512))
            ELSE CAST('skills/_private/' + owner_upn + '/' + skill_name + '/' AS VARCHAR(512))
        END
    ) PERSISTED;

    PRINT N'[v2.1] dbo.skills.blob_prefix 已新增。';
END
ELSE
BEGIN
    PRINT N'[v2.1] dbo.skills.blob_prefix 已存在,略過。';
END
GO


-- ============================================================
-- 2. v_my_skills 加開 blob_prefix
-- ============================================================
-- 讀路徑(skills_provider_factory._fetch_allowed_skills())是自列欄位的 explicit
-- SELECT + 欄位名稱取值,本 view 的欄位順序不影響它;但「新增/更名欄位」時
-- 仍需同步該處的 SELECT 清單,否則新欄位查不到。

CREATE OR ALTER VIEW [dbo].[v_my_skills] AS
SELECT
    s.skill_name,
    s.owner_upn,
    s.is_public,
    CASE WHEN s.owner_upn IS NULL THEN 'global' ELSE 'private' END AS skill_scope,
    s.skill_key,
    s.blob_path,
    s.blob_prefix,
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
--   - 全域技能 blob_prefix = 'skills/{skill_name}/'
--   - 私有技能 blob_prefix = 'skills/_private/{owner_upn}/{skill_name}/'
--   - 每一列都滿足 blob_path = blob_prefix + 'SKILL.md'
--   - 每一列的 blob_prefix 都以 '/' 結尾

-- 3.1 目視檢查
SELECT skill_name, owner_upn, skill_key, blob_path, blob_prefix
FROM dbo.skills;
GO

-- 3.2 一致性斷言 — 應回傳 0 列。回傳任何列都代表公式漏改。
SELECT skill_name, owner_upn, blob_path, blob_prefix
FROM dbo.skills
WHERE blob_path <> blob_prefix + 'SKILL.md'
   OR RIGHT(blob_prefix, 1) <> '/';
GO

-- 3.3 以一般使用者身份連線跑,確認 RLS 過濾未受影響且新欄位有值
-- SELECT * FROM dbo.v_my_skills;
