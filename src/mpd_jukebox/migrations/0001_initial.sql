-- Key/value metadata: 'mode' and 'constraint_fingerprint'.
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The jukebox queue: songs or albums picked in advance.
CREATE TABLE jukebox_queue (
    pos       INTEGER PRIMARY KEY,
    key       TEXT NOT NULL,    -- song URI or album key
    lineno    INTEGER NOT NULL,
    tag_value TEXT NOT NULL
);

-- Songs that reached scrobble time; newest has the highest id.
CREATE TABLE history (
    id  INTEGER PRIMARY KEY,
    uri TEXT NOT NULL,
    ts  INTEGER NOT NULL
);
