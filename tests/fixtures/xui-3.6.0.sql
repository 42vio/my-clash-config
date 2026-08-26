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
CREATE TABLE inbounds (id INTEGER PRIMARY KEY, port INTEGER NOT NULL, protocol TEXT NOT NULL, enable INTEGER NOT NULL, listen TEXT NOT NULL, settings TEXT NOT NULL, stream_settings TEXT NOT NULL, remark TEXT NOT NULL);
INSERT INTO inbounds VALUES (1, 10443, 'vless', 1, '0.0.0.0', '{}', '{"security":"reality","realitySettings":{"serverName":"www.example.com"}}', 'reality-main');
INSERT INTO settings VALUES ('webPort', '2053');
INSERT INTO settings VALUES ('webBasePath', '/xui7k2m/');
INSERT INTO settings VALUES ('webListen', '127.0.0.1');
