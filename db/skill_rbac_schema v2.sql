-- ============================================================
-- EAA Skill RBAC — Azure SQL / Fabric SQL Schema (Mode B, v2)
-- ============================================================
-- 適用對象:全新部署(Greenfield)。
-- 本檔案不是既有系統的 migration script——若客戶已在跑 v1
-- schema(skill_name 當 PK),需要另外的遷移腳本,不在此檔範圍內。
--
-- 與 v1 的核心差異:
--   - 新增 owner_upn(nullable):NULL = 全域/系統技能;有值 = 該
--     user 自建的私有技能(DA 情境專用)
--   - 新增 is_public(bit):1 = 對所有使用者公開,不需要逐一 grant。
--     這是「技能本身的屬性」,不是逐人判斷的可見性規則,因此直接在
--     view 的 WHERE 子句處理,不走 user_skill_grants / RLS
--   - PK 改用 computed column `skill_key`(= skill_name,或
--     skill_name + '_' + owner_upn),取代原本單純的 skill_name PK
--   - blob_path 也改為 computed column(見下方說明),不再是可被
--     顯式賦值的一般欄位
--   - 2026.08.14 George : 新增 blob_prefix computed column —— skill 資料夾
--     格式(SKILL.md + references/ + assets/)支援。blob_path 指向單一檔案,
--     無法用來 list_blobs 列舉整包;blob_prefix 補上這個能力。blob_path
--     刻意保留不動,既有讀路徑零改動
--   - 2026.08.21 George : 新增 is_internal —— Skill Registry 分階(情境層
--     parent / 能力層 internal child)。標記為 internal 的 skill 不出現在
--     對外 skill 目錄,但 runtime 仍載得到,由 parent 正文指名。
--     過濾「只」發生在應用層投影,view 的 WHERE 與 RLS predicate 皆不動
--     (理由見 `skill_rbac_schema v2.2 migration.sql`)
--   - 因為 skill_key 本身已保證全表唯一,不需要額外的 filtered
--     unique index 來分層擋同名
--   - user_skill_grants 的 FK 改指向 skill_key(而非 skill_name)
--   - 移除 version 欄位:Mode B 固定覆蓋 latest,寫路徑只能塞字面
--     'latest' 搞定 NOT NULL,實質一直是死欄位
--   - 移除 description 欄位:description 以 Blob SKILL.md frontmatter 為
--     唯一來源。舊 schema 兩邊各存一份,只要 publish 漏更新 SQL 就會
--     drift;拿掉後「單一來源」從約定變成 schema 強制
--
-- 部署順序:
--   1. 用 admin 身份連 Azure SQL / Fabric SQL,執行此檔
--   2. 確保 admin 在 'SkillAdmins' role 內(用於後續 Gatekeeper 寫入)
--   3. 跑檔案下方的 seed 資料測試
--
-- 注意:
--   - 此 schema 假設 OBO passthrough,user 連線時 SUSER_NAME() 回傳 UPN
--   - skill_name 命名規則對齊 agent_framework._skills VALID_NAME_RE
--     (僅允許小寫字母/數字/hyphen),因此 skill_name 永遠不含 `_` 或 `@`,
--     這是 skill_key/blob_path 用 `_`/`/` 當分隔符、且不會與 UPN(必含 `@`)
--     混淆撞名的前提
--   - owner_upn 改名(如帳號更名/租戶遷移)的處理不在本次設計範圍內
--     (機率極低,暫不考慮;若日後需要,skill_key/blob_path 皆為
--      computed column,需另外評估 ON UPDATE CASCADE 對 computed PK
--      的行為,以及既有 Blob 檔案的實體搬遷)
--   - 【99% 人工 INSERT、1% 程式化寫入的環境定案】：dbo.skills 這張表
--     的寫入,絕大多數是 DBA/管理員手動執行 INSERT(例如發布新的全域
--     技能),只有在「判斷需要自動新增」的情境(self-service 私有技能)
--     才會走程式化路徑。這個事實是把 skill_key、blob_path
--     都做成 computed column 的關鍵理由:人工輸入時只需要提供
--     skill_name / owner_upn 這兩個基本欄位,由 DB 自動推導出識別鍵與
--     儲存路徑,不需要手動重打容易出錯的組合字串。命名規則若日後需要
--     改版,屬於低頻、可規劃的遷移事件,權衡之下代價低於「每一次人工
--     寫入都要正確手打長路徑字串」這個高頻風險。
-- ============================================================


-- ===== Cleanup(僅 dev 環境用,production 別跑)=====
-- IF EXISTS (SELECT * FROM sys.security_policies WHERE name = 'UserSkillGrantsPolicy')
--     DROP SECURITY POLICY UserSkillGrantsPolicy;
-- DROP VIEW IF EXISTS v_my_skills;
-- DROP FUNCTION IF EXISTS fn_user_skill_grants_predicate;
-- DROP TABLE IF EXISTS user_skill_grants;
-- DROP TABLE IF EXISTS skills;


-- ============================================================
-- 0. 清理舊有物件 (依賴關係反向刪除)
-- ============================================================

IF EXISTS (SELECT * FROM sys.security_policies WHERE name = 'UserSkillGrantsPolicy')
BEGIN
    DROP SECURITY POLICY UserSkillGrantsPolicy;
END
GO

-- v_my_skills 參照 skills,fn_user_skill_grants_predicate 是 SCHEMABINDING
-- 綁在 user_skill_grants 上——兩者都必須先於表被拆掉。
DROP VIEW IF EXISTS dbo.v_my_skills;
GO

DROP FUNCTION IF EXISTS dbo.fn_user_skill_grants_predicate;
GO

IF EXISTS (SELECT * FROM sys.indexes WHERE name = 'IX_user_skill_grants_skill' AND object_id = OBJECT_ID('dbo.user_skill_grants'))
BEGIN
    DROP INDEX IX_user_skill_grants_skill ON dbo.user_skill_grants;
END
GO

IF EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID('dbo.user_skill_grants') AND type = 'U')
BEGIN
    DROP TABLE dbo.user_skill_grants;
END
GO

IF EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID('dbo.skills') AND type = 'U')
BEGIN
    DROP TABLE dbo.skills;
END
GO


-- ============================================================
-- 1. Skill metadata table
-- ============================================================
-- owner_upn:
--   NULL     -> 全域/系統技能(現有 Auto Refine / Skill Gatekeeper 流程,
--               不傳這個欄位時預設為 NULL,行為與純 EAA、無 DA 的客戶完全一致)
--   有值     -> 該 user 透過 DA 自建的私有技能,值為 SUSER_SNAME() 格式的 UPN
--
-- skill_key(computed, PERSISTED)——DB 內部識別鍵,PK/FK 用:
--   owner_upn IS NULL -> skill_key = skill_name                          (例:azure-diagrams-tips)
--   owner_upn 有值     -> skill_key = skill_name + '_' + owner_upn        (例:azure-diagrams-tips_alice@microsoft.com)
--
-- blob_path(computed, PERSISTED)——SKILL.md 這個「入口檔」的實際存放路徑:
--   owner_upn IS NULL -> 'skills/' + skill_name + '/SKILL.md'
--   owner_upn 有值     -> 'skills/_private/' + owner_upn + '/' + skill_name + '/SKILL.md'
--   兩者用同一組輸入(skill_name、owner_upn)推導,人工 INSERT 時完全
--   不需要手動輸入這兩個欄位,天生不會打錯字。
--
-- blob_prefix(computed, PERSISTED)——2026.08.14 George : skill 資料夾的
--   Blob prefix,等同 blob_path 去掉結尾的 'SKILL.md':
--   owner_upn IS NULL -> 'skills/' + skill_name + '/'
--   owner_upn 有值     -> 'skills/_private/' + owner_upn + '/' + skill_name + '/'
--   用途:讓讀路徑能 list_blobs(name_starts_with=blob_prefix) 列舉整個 skill
--   資料夾(references/、assets/ 等),而不是只拿得到 SKILL.md 一個檔。
--   ⚠️ 結尾一定要有 '/',否則 'skills/foo/' 的列舉會誤中 'skills/foobar/'。
--   刻意不改 blob_path 的定義:既有讀路徑(_fetch_skill_content()、
--   list_allowed_skills_full())繼續直取 SKILL.md,完全不受影響。
--
-- ⚠️ blob_path / blob_prefix 是 computed column,只能在「列已存在」之後讀得到。
--   寫路徑(先上傳 Blob、再 INSERT metadata)在上傳的那一刻還沒有列可讀,因此
--   必須在應用端以相同公式自行推導上傳目的地(見 skills_sync.
--   skill_blob_path() / skill_blob_prefix())。這份「鏡射公式」是刻意的重複:
--   任何一邊改命名規則,另一邊必須同步,否則 Mode B 讀路徑會拿到指不到檔案的路徑。
--
--
-- 因為 skill_name 的 CHECK constraint 只允許 [a-z0-9-],不可能出現 `_`、
-- `@` 或 `/`,而 UPN 必定含 `@`,所以「全域」與「私有」在字元集上是
-- 結構性互斥的兩個命名空間,skill_key 與 blob_path 都不會誤撞。

CREATE TABLE skills (
    skill_name      VARCHAR(64)    NOT NULL,
    owner_upn       VARCHAR(256)   NULL,
    is_public       BIT            NOT NULL DEFAULT 0,     -- 1=對所有使用者公開,不需逐一 grant
    -- 2026.08.21 George : 1=不出現在對外 skill 目錄,只能由 parent skill 指名載入。
    -- 與 is_public 正交,不是反義詞:is_public 管身分授權,is_internal 管目錄拓樸,
    -- 兩者可同時為 1。過濾在應用層做,view 的 WHERE 不含這個欄位。
    is_internal     BIT            NOT NULL DEFAULT 0,
    -- version         VARCHAR(32)    NOT NULL,
    -- description     NVARCHAR(1024) NOT NULL,
    enabled         BIT            NOT NULL DEFAULT 1,
    created_at      DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at      DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),

    skill_key AS (
        CASE
            WHEN owner_upn IS NULL THEN CAST(skill_name AS VARCHAR(321))
            ELSE CAST(skill_name + '_' + owner_upn AS VARCHAR(321))
        END
    ) PERSISTED,

    blob_path AS (
        CASE
            WHEN owner_upn IS NULL
                THEN CAST('skills/' + skill_name + '/SKILL.md' AS VARCHAR(512))
            ELSE CAST('skills/_private/' + owner_upn + '/' + skill_name + '/SKILL.md' AS VARCHAR(512))
        END
    ) PERSISTED,

    -- 2026.08.14 George : skill 資料夾列舉用。結尾的 '/' 是必要的。
    blob_prefix AS (
        CASE
            WHEN owner_upn IS NULL
                THEN CAST('skills/' + skill_name + '/' AS VARCHAR(512))
            ELSE CAST('skills/_private/' + owner_upn + '/' + skill_name + '/' AS VARCHAR(512))
        END
    ) PERSISTED,

    CONSTRAINT PK_skills PRIMARY KEY (skill_key),

    CONSTRAINT CK_skill_name_format CHECK (
        skill_name NOT LIKE '%[^a-z0-9-]%'
        AND skill_name NOT LIKE '-%'
        AND skill_name NOT LIKE '%-'
        AND LEN(skill_name) <= 64
    ),

    -- 預設不允許「私有技能同時公開給所有人」這個組合,避免語意矛盾。
    -- 若日後要做「使用者一鍵把私有技能分享給全公司」,再拿掉這條 constraint。
    CONSTRAINT CK_skills_is_public_scope CHECK (is_public = 0 OR owner_upn IS NULL)
);
GO

-- ============================================================
-- 2. User → Skill grants table
-- ============================================================
-- FK 改指向 skills.skill_key(而非 v1 的 skill_name),因為 skill_name
-- 在本 schema 已不具全表唯一性,無法再被參照。

CREATE TABLE user_skill_grants (
    user_upn        VARCHAR(256)  NOT NULL,
    skill_key       VARCHAR(321)  NOT NULL,
    granted_at      DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
    granted_by      VARCHAR(256)  NULL,
    expires_at      DATETIME2     NULL,
    CONSTRAINT PK_user_skill_grants PRIMARY KEY (user_upn, skill_key),
    CONSTRAINT FK_user_skill_grants_skill
        FOREIGN KEY (skill_key) REFERENCES skills(skill_key) ON DELETE CASCADE
);
GO

CREATE INDEX IX_user_skill_grants_skill ON user_skill_grants(skill_key);
GO


-- ============================================================
-- 3. RLS Predicate Function
-- ============================================================
-- 注意:可見性判斷分兩層——(1) 因人而異的授權,收斂在 user_skill_grants
-- 這張表上,由這個 predicate 判斷;(2) 對所有人公開的技能(is_public=1),
-- 這不是「因人而異」的判斷,不需要也不透過 RLS,直接在 v_my_skills 的
-- WHERE 子句處理(見第 7 節)。兩者職責分開,predicate 本身維持單純。

CREATE OR ALTER FUNCTION fn_user_skill_grants_predicate(@row_upn VARCHAR(256))
RETURNS TABLE
WITH SCHEMABINDING
AS RETURN
    SELECT 1 AS allowed
    WHERE @row_upn = SUSER_SNAME()                                  -- 一般 user:自比對(含私有技能的自建 grant)
       -- OR IS_MEMBER('SkillAdmins') = 1                            -- 預留 group 機制
       OR SUSER_SNAME() = '[YOUR-ACA-APP-NAME-HERE]';                -- ACA MI(寫路徑)
GO


-- ============================================================
-- 4. Security Policy
-- ============================================================

CREATE SECURITY POLICY UserSkillGrantsPolicy
ADD FILTER PREDICATE
    dbo.fn_user_skill_grants_predicate(user_upn)
    ON dbo.user_skill_grants
WITH (STATE = ON);
GO


-- ============================================================
-- 5. Seed data — 測試用
-- ============================================================
-- 全域技能(owner_upn 不指定,預設 NULL)。
-- 注意:INSERT 欄位清單不再包含 blob_path / blob_prefix——它們是 computed
-- column,顯式指定會直接被 SQL Server 拒絕。DB 會自動推導出正確路徑。
--
-- ⚠️ 這裡只 seed 一筆範例資料用來驗證 schema。實際部署時,請依貴公司
--   自己發布的技能清單逐筆 INSERT(skill_name 必須跟 Blob 上的目錄名
--   一致);不要直接把本專案附的其他範例技能一併建入。
INSERT INTO skills (skill_name, is_public, enabled, created_at, updated_at) VALUES
    ('azure-diagrams-tips', 0, 1, SYSUTCDATETIME(), SYSUTCDATETIME())
GO    
-- 私有技能範例(Only for DA 情境):alice 自建了一個跟全域 azure-diagrams-tips 同名、
-- 但屬於自己的版本,示範同名共存合法、彼此不撞鍵
INSERT INTO skills (skill_name, owner_upn, is_public, enabled, created_at, updated_at) VALUES
    ('azure-diagrams-tips', 'alice@microsoft.com', 0, 1, SYSUTCDATETIME(), SYSUTCDATETIME())
GO

-- 驗證 computed column 是否如預期推導(seed 完馬上跑一次確認)
-- SELECT skill_name, owner_upn, skill_key, blob_path, blob_prefix FROM skills;


-- ========================================================================
-- 6. 授權配置(grant)流程說明
-- ========================================================================
--
-- ------------------------------------------------------------------------
-- 6.1 準備識別字串(user_upn)
-- ------------------------------------------------------------------------
-- [情境 A:一般使用者] 直接使用組織 Email 帳號(例:admin@MngEnvMCAP352952.onmicrosoft.com)
-- [情境 B:應用程式/受控識別] SUSER_SNAME() 格式為 ClientID@TenantID,取得方式:
--   "$(az ad sp list --display-name your-app_name --query '[0].appId' -o tsv)@$(az account show --query tenantId -o tsv)"
--
-- ------------------------------------------------------------------------
-- 6.2 全域技能:個別 user grant(現有流程,行為不變)
-- ------------------------------------------------------------------------
DECLARE @user_upn NVARCHAR(255) = '請在這邊貼上您的 Email 或 PowerShell 產出的 AppID 字串';
DECLARE @granted_by NVARCHAR(100) = 'gatekeeper-seed';

IF @user_upn LIKE '%請在這邊貼上%' OR @user_upn = ''
BEGIN
    RAISERROR(N'錯誤：請先將 @user_upn 變數修改為實際的 Email 或 AppID 組合字串！', 16, 1);
    RETURN;
END

INSERT INTO user_skill_grants (user_upn, skill_key, granted_by)
VALUES
    (@user_upn, 'azure-diagrams-tips', @granted_by);          -- 對應全域技能,skill_key = skill_name


PRINT N'權限配置成功！已指派給: ' + @user_upn;
GO

-- ------------------------------------------------------------------------
-- 6.3 全域可用 Skill(Public Skill,is_public=1)
-- ------------------------------------------------------------------------
-- 不需要 grant row。單純把該技能標成公開,v_my_skills 的 WHERE 子句
-- 會讓所有使用者都看到它(見第 7 節)。
-- 建議:僅由 SKILL_ADMIN_UPN 對應帳號或指定 DBA 角色手動執行,不對一般
--    Gatekeeper 審核流程開放——公開範圍是全體使用者,影響層級不同於
--    單一 user 授權。

UPDATE skills
SET is_public = 1
WHERE skill_key = 'azure-diagrams-tips';   -- skill_key = skill_name(全域技能)

-- 撤銷:
-- UPDATE skills SET is_public = 0 WHERE skill_key = 'azure-diagrams-tips';
GO


-- ============================================================
-- 7. Create View
-- ============================================================
-- 額外暴露 owner_upn / skill_scope / is_public,方便應用層/前端判斷這是
-- 全域、使用者私有、還是公開技能。blob_path / blob_prefix 是 computed column,
-- SELECT 方式跟一般欄位完全一樣,不影響這個 view 的寫法。
--
-- 2026.08.14 George : 新增 blob_prefix 欄位供 Mode B 列舉整個 skill 資料夾。
-- 讀路徑(skills_provider_factory._fetch_allowed_skills())是自列欄位的 explicit
-- SELECT + 欄位名稱取值,本 view 的欄位順序不影響它;但「新增/更名欄位」時
-- 仍需同步該處的 SELECT 清單,否則新欄位查不到。
--
-- 2026.08.21 George : 新增 is_internal 欄位。這裡「只是把欄位交出去」——
-- WHERE 子句刻意不加 is_internal 條件:本 view 是對外 list_skills 與 MAF
-- runtime materialize(create_provider_scope())共用的唯一查詢,在 SQL 端過濾
-- 會讓 internal skill 連 runtime 都載不進來,parent 指名後就拿不到了。
-- 過濾只在 core_handler.list_skills() 的投影做。
--
-- 可見性規則(注意 enabled 用 AND 包住整個 OR,避免公開技能被下架後
-- 仍對所有人可見這種 bug):
--   enabled = 1 AND (
--       有對應的 user_skill_grants row(且未過期)
--    OR is_public = 1
--   )

CREATE OR ALTER VIEW [dbo].[v_my_skills] AS
SELECT
    s.skill_name,
    s.owner_upn,
    s.is_public,
    CASE WHEN s.owner_upn IS NULL THEN 'global' ELSE 'private' END AS skill_scope,
    s.skill_key,
    --'' AS description,
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

-- 讀路徑(SkillsProviderFactory)只查這個 view。一般使用者不需要、也不應該
-- 拿到底層兩張表的 SELECT 權限。請依實際使用的 role/帳號自行調整。
-- GRANT SELECT ON [dbo].[v_my_skills] TO [請填入 role 或使用者];
-- GO


-- ============================================================
-- 8. 驗證查詢(用您的身份連線跑這個)
-- ============================================================
-- 預期:
--   - 有 grant 的 user 看到全域技能 + 自己建立的私有技能 + 所有 is_public=1 的技能
--   - 完全不在 grants 表的 user,仍會看到所有 is_public=1 的技能
--     (v_my_skills 對 skills 改用 LEFT JOIN,is_public 路徑不需要 grant row)
--   - 若同一個 skill_name 同時存在全域版與該 user 的私有版(如 alice 的
--     azure-diagrams-tips),v_my_skills 會回傳「兩筆」——執行期由
--     skills_provider_factory.py 依 skill_name 做 dedupe,private 優先於
--     global;此 view 本身不做 tie-break。
--   - blob_path 應該自動等於「skills/{skill_name}/SKILL.md」(全域)或
--     「skills/_private/{owner_upn}/{skill_name}/SKILL.md」(私有),
--     不需要也不應該手動核對——computed column 保證一致。
--   - blob_prefix 應該等於 blob_path 去掉結尾的 'SKILL.md',且必須以 '/' 結尾。
SELECT * FROM v_my_skills;