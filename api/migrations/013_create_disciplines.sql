CREATE TABLE IF NOT EXISTS disciplines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO disciplines (name) VALUES
    ('Hard stop-loss set'),
    ('Waited for retest'),
    ('Followed the plan')
ON CONFLICT (name) DO NOTHING;
