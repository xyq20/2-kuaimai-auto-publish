CREATE TABLE IF NOT EXISTS model_learning_rules (
 id TEXT PRIMARY KEY,
 scope_key TEXT NOT NULL,
 scope_json TEXT NOT NULL,
 evidence_json TEXT NOT NULL,
 evidence_version TEXT NOT NULL,
 status TEXT NOT NULL,
 proposal_json TEXT NOT NULL,
 validation_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_rule_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT NOT NULL, user_id TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS model_learning_rules_scope ON model_learning_rules(scope_key, updated_at);
