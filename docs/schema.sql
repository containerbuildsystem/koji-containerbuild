
-- vim:noet:sw=8
-- still needs work

DROP TABLE IF EXISTS container_builds;

-- track container builds
CREATE TABLE container_builds (
    build_id INTEGER NOT NULL PRIMARY KEY REFERENCES build(id)
) WITHOUT OIDS;
