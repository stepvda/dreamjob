-- ===========================================================================
-- Financial analysis: balance-sheet detail and group structure
-- (FR-243, FR-246, NFR-404, DR-103)
--
-- 001_initial.sql gives financial_year the income-statement figures and the
-- headline balance-sheet items, and financial_analysis a current_ratio column.
-- A current ratio cannot be computed from equity, cash and total debt alone,
-- and NFR-404's balance-sheet reconciliation needs the totals it is checking
-- against, so the four missing aggregates are added here.
--
-- company_group_link carries FR-246: a registry that exposes the parent /
-- subsidiary relation records it, and the consolidated picture is then an
-- aggregation over the group rather than a second, conflicting financial_year
-- row (the UNIQUE (company_id, fiscal_year) key rules that out by design).
-- ===========================================================================

ALTER TABLE financial_year ADD COLUMN total_assets REAL;
ALTER TABLE financial_year ADD COLUMN current_assets REAL;
ALTER TABLE financial_year ADD COLUMN current_liabilities REAL;
ALTER TABLE financial_year ADD COLUMN gross_profit REAL;

CREATE TABLE company_group_link (
    id                    TEXT PRIMARY KEY,
    parent_company_id     TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    subsidiary_company_id TEXT REFERENCES company(id) ON DELETE CASCADE,
    -- Registries name subsidiaries long before those subsidiaries are profiled,
    -- so the link keeps the raw identity as well as the resolved row.
    subsidiary_key        TEXT NOT NULL,          -- company id | legal id | normalised name
    subsidiary_name       TEXT,
    subsidiary_legal_id   TEXT,
    relation              TEXT NOT NULL DEFAULT 'subsidiary',  -- subsidiary|branch|participation
    ownership_pct         REAL,
    source                TEXT,
    collected_at          TEXT NOT NULL,
    UNIQUE (parent_company_id, subsidiary_key)
);
CREATE INDEX idx_group_parent ON company_group_link(parent_company_id);
CREATE INDEX idx_group_sub ON company_group_link(subsidiary_company_id);
