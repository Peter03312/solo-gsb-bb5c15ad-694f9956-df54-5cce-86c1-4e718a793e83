-- 失物认领系统数据库结构（SQLite）
-- 幂等：重复执行不会破坏已有数据。

-- 物品：公开描述对访客可见；私密特征仅工作人员可见。
CREATE TABLE IF NOT EXISTS items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    public_description TEXT    NOT NULL,
    private_features   TEXT    NOT NULL,
    -- kept=保管中  removed=已撤下  returned=已交还
    status             TEXT    NOT NULL DEFAULT 'kept'
                         CHECK (status IN ('kept', 'removed', 'returned')),
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

-- 认领申请：token_hash 存放查询凭证的 SHA-256，凭证明文只出现一次。
CREATE TABLE IF NOT EXISTS claims (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES items(id),
    answer     TEXT    NOT NULL,           -- 访客提交的辨认答案（仅工作人员可见）
    contact    TEXT    NOT NULL,           -- 联系方式（仅工作人员可见）
    token_hash TEXT    NOT NULL UNIQUE,
    -- pending=待核对  selected=已获选  rejected=未获选
    status     TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'selected', 'rejected')),
    created_at TEXT    NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_item ON claims(item_id);

-- 一次性交还码：code_hash 用于核码查找，code_enc 为对称加密密文，
-- 仅授权页面（持凭证的获选人 / 工作人员）解密回显。
CREATE TABLE IF NOT EXISTS handover_codes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id       INTEGER NOT NULL REFERENCES claims(id),
    code_hash      TEXT    NOT NULL,
    code_enc       TEXT    NOT NULL,
    issued_at      TEXT    NOT NULL,
    expires_at     TEXT    NOT NULL,
    consumed_at    TEXT,                   -- 核销时间
    invalidated_at TEXT                    -- 被新码作旧的时间
);
CREATE INDEX IF NOT EXISTS idx_codes_claim ON handover_codes(claim_id);
CREATE INDEX IF NOT EXISTS idx_codes_hash ON handover_codes(code_hash);
