-- On-chain alerts already turned into Event nodes, so a rejected one is not proposed again.
CREATE TABLE IF NOT EXISTS onchain_alerts (
    tx          text PRIMARY KEY,
    node_id     text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
