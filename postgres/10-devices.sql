CREATE TABLE devices (
    id INTEGER PRIMARY KEY,
    name VARCHAR(128),
    last_seen TIMESTAMP DEFAULT NOW() NOT NULL
);

CREATE UNIQUE INDEX devices_name_idx ON devices (name);
