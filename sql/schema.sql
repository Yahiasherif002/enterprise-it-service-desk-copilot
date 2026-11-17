IF OBJECT_ID('dbo.service_request') IS NULL
CREATE TABLE dbo.service_request (
    request_id    nvarchar(100)  NOT NULL CONSTRAINT pk_service_request PRIMARY KEY,
    requester     nvarchar(320)  NOT NULL,
    summary       nvarchar(160)  NOT NULL,
    details       nvarchar(4000) NOT NULL,
    category      varchar(20)    NOT NULL CONSTRAINT ck_service_request_category
                  CHECK (category IN ('hardware', 'software', 'network', 'database', 'inquiry')),
    urgency       tinyint        NOT NULL CONSTRAINT ck_service_request_urgency CHECK (urgency BETWEEN 1 AND 3),
    impact        tinyint        NOT NULL CONSTRAINT ck_service_request_impact CHECK (impact BETWEEN 1 AND 3),
    status        varchar(20)    NOT NULL CONSTRAINT ck_service_request_status
                  CHECK (status IN ('pending_approval', 'rejected', 'ready', 'creating', 'created', 'failed')),
    approver      nvarchar(320)  NULL,
    ticket_number varchar(40)    NULL,
    ticket_sys_id char(32)       NULL,
    created_at    datetime2(0)   NOT NULL CONSTRAINT df_service_request_created_at DEFAULT SYSUTCDATETIME(),
    updated_at    datetime2(0)   NOT NULL CONSTRAINT df_service_request_updated_at DEFAULT SYSUTCDATETIME()
);

IF OBJECT_ID('dbo.audit_event') IS NULL
CREATE TABLE dbo.audit_event (
    id         bigint IDENTITY CONSTRAINT pk_audit_event PRIMARY KEY,
    request_id nvarchar(100)  NOT NULL CONSTRAINT fk_audit_event_request REFERENCES dbo.service_request (request_id),
    event      varchar(30)    NOT NULL,
    actor      nvarchar(320)  NOT NULL,
    detail     nvarchar(4000) NULL,
    at         datetime2(0)   NOT NULL CONSTRAINT df_audit_event_at DEFAULT SYSUTCDATETIME()
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_service_request_requester')
CREATE INDEX ix_service_request_requester ON dbo.service_request (requester, created_at DESC);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_audit_event_request')
CREATE INDEX ix_audit_event_request ON dbo.audit_event (request_id, id);

IF DATABASE_PRINCIPAL_ID('servicedesk_app') IS NULL
EXEC ('CREATE ROLE servicedesk_app');

GRANT SELECT, INSERT, UPDATE ON dbo.service_request TO servicedesk_app;
GRANT SELECT, INSERT ON dbo.audit_event TO servicedesk_app;
