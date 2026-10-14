-- Run as a DBA in the EAA database. Runtime never executes this DDL.
IF OBJECT_ID('dbo.skill_neighbor_notice', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.skill_neighbor_notice (
        id BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_skill_neighbor_notice PRIMARY KEY,
        skill_key VARCHAR(321) NOT NULL,
        target_user VARCHAR(256) NOT NULL,
        proposer_upn VARCHAR(256) NOT NULL,
        source_skill_keys_json NVARCHAR(MAX) NOT NULL,
        status VARCHAR(16) NOT NULL CONSTRAINT DF_skill_neighbor_notice_status DEFAULT ('open'),
        created_at DATETIME2(7) NOT NULL CONSTRAINT DF_skill_neighbor_notice_created_at DEFAULT SYSUTCDATETIME(),
        updated_at DATETIME2(7) NOT NULL CONSTRAINT DF_skill_neighbor_notice_updated_at DEFAULT SYSUTCDATETIME(),
        closed_at DATETIME2(7) NULL,
        modify_session_id VARCHAR(64) NULL,
        CONSTRAINT CK_skill_neighbor_notice_status CHECK (status IN ('open', 'accepted', 'dismissed')),
        CONSTRAINT CK_skill_neighbor_notice_sources_json CHECK (ISJSON(source_skill_keys_json) = 1)
    );
    CREATE UNIQUE INDEX UX_skill_neighbor_notice_open
        ON dbo.skill_neighbor_notice (skill_key, proposer_upn) WHERE status = 'open';
    CREATE INDEX IX_skill_neighbor_notice_target_status_created
        ON dbo.skill_neighbor_notice (target_user, status, created_at DESC);
END;
GO

-- Replace the principal name with the existing Generator database user.
-- No DELETE, DDL, database role, or end-user SQL permission is required.
GRANT SELECT, INSERT, UPDATE ON OBJECT::dbo.skill_neighbor_notice
    TO [YOUR-SKILL-GENERATOR-SP-NAME-HERE];
GO