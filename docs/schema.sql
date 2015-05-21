
-- vim:noet:sw=8
-- still needs work

INSERT INTO channels (name) VALUES ('container');

DROP TABLE IF EXISTS container_builds;
DROP TABLE IF EXISTS container_archives;
DROP TABLE IF EXISTS container_listing;

-- track container builds
CREATE TABLE container_builds (
    build_id INTEGER NOT NULL PRIMARY KEY REFERENCES build(id)
) WITHOUT OIDS;

INSERT INTO archivetypes (name, description, extensions) values ('container', 'Container image', 'tar.gz');

CREATE TABLE container_archives (
    archive_id INTEGER NOT NULL PRIMARY KEY REFERENCES archiveinfo(id),
    arch VARCHAR(16) NOT NULL
) WITHOUT OIDS;

-- tracks the contents of an container
CREATE TABLE container_listing (
	container_id INTEGER NOT NULL REFERENCES container_archives(archive_id),
	rpm_id INTEGER NOT NULL REFERENCES rpminfo(id),
	UNIQUE (container_id, rpm_id)
) WITHOUT OIDS;
CREATE INDEX container_listing_rpms on container_listing(rpm_id);
