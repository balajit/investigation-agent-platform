CREATE TABLE payments (
    id          TEXT PRIMARY KEY,
    amount_cents INTEGER NOT NULL,
    status      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
