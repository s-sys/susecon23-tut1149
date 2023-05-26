CREATE TABLE reboot_requests (
    id SERIAL PRIMARY KEY,
    itsm_id VARCHAR(64) NOT NULL,
    device_name VARCHAR(128) NOT NULL,
    action_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT NOW() NOT NULL
);

CREATE INDEX reboot_requests_itsm_id_idx ON reboot_requests (itsm_id);
CREATE INDEX reboot_requests_device_name_idx ON reboot_requests (device_name);
CREATE INDEX reboot_requests_action_id_idx ON reboot_requests (action_id);
