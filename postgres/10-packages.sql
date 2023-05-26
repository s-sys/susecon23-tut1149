CREATE TABLE packages (
    id INTEGER PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    version VARCHAR(128)
);

CREATE INDEX packages_name_idx ON packages (name);
CREATE UNIQUE INDEX packages_version_idx ON packages (name, version);
