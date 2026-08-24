CREATE TABLE clients (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  sub_id TEXT NOT NULL UNIQUE,
  enable NUMERIC NOT NULL,
  total_gb INTEGER NOT NULL,
  expiry_time INTEGER NOT NULL
);
CREATE TABLE client_traffics (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  up INTEGER NOT NULL,
  down INTEGER NOT NULL
);
CREATE TABLE settings (`key` TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO settings VALUES ('subListen', '127.0.0.1');
INSERT INTO settings VALUES ('subPort', '2096');
INSERT INTO settings VALUES ('subEnable', 'true');
INSERT INTO settings VALUES ('subClashEnable', 'true');
INSERT INTO settings VALUES ('subClashPath', '/clash/');
