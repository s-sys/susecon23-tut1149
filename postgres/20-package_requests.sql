CREATE TABLE package_requests (
    id SERIAL PRIMARY KEY,
    itsm_id VARCHAR(64) NOT NULL,
    device_name VARCHAR(128) NOT NULL,
    package_name VARCHAR(128) NOT NULL,
    package_version VARCHAR(128),
    after TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT NOW() NOT NULL,
    reverted BOOLEAN DEFAULT FALSE NOT NULL
);

CREATE INDEX package_requests_itsm_id_idx ON package_requests (itsm_id);
CREATE INDEX package_requests_device_name_idx ON package_requests (device_name);
CREATE INDEX package_requests_package_name_idx ON package_requests (device_name, package_name);
CREATE INDEX package_requests_reverted_idx ON package_requests (reverted);
CREATE UNIQUE INDEX package_requests_itsm_unique_idx ON package_requests (itsm_id, device_name, package_name, package_version);
