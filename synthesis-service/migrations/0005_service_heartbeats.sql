CREATE TABLE IF NOT EXISTS synthesis_service_heartbeats (
    service_name TEXT NOT NULL,
    instance_id UUID NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (service_name, instance_id)
);
