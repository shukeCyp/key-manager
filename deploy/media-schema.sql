-- Seven-table media catalog, utf8mb4, MySQL 8.0.

-- Original cue text is permanent; search_* fields are rebuildable cache.

CREATE TABLE files (
	id VARCHAR(64) NOT NULL,
	path_hash VARCHAR(64) NOT NULL,
	path TEXT NOT NULL,
	kind VARCHAR(16) NOT NULL,
	format VARCHAR(32) NOT NULL,
	size BIGINT,
	mtime_ns BIGINT,
	duration_ms BIGINT,
	availability VARCHAR(24) NOT NULL,
	probe_status VARCHAR(24) NOT NULL,
	error TEXT,
	details MEDIUMTEXT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (path_hash)
);

CREATE INDEX ix_files_kind ON files (kind);

CREATE TABLE works (
	id VARCHAR(32) NOT NULL,
	identity_key VARCHAR(64) NOT NULL,
	title VARCHAR(255) NOT NULL,
	title_zh VARCHAR(255),
	title_original VARCHAR(255),
	kind VARCHAR(16) NOT NULL,
	year INTEGER,
	status VARCHAR(24) NOT NULL,
	confidence FLOAT NOT NULL,
	details MEDIUMTEXT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (identity_key)
);

CREATE TABLE episodes (
	id VARCHAR(32) NOT NULL,
	work_id VARCHAR(32) NOT NULL,
	code VARCHAR(40) NOT NULL,
	season INTEGER,
	number INTEGER,
	title VARCHAR(255),
	PRIMARY KEY (id),
	FOREIGN KEY(work_id) REFERENCES works (id)
);

CREATE INDEX ix_episodes_work_id ON episodes (work_id);

CREATE UNIQUE INDEX uq_media_episode ON episodes (work_id, code);

CREATE TABLE tracks (
	id VARCHAR(32) NOT NULL,
	file_id VARCHAR(64) NOT NULL,
	kind VARCHAR(16) NOT NULL,
	source VARCHAR(16) NOT NULL,
	stream_key VARCHAR(32) NOT NULL,
	stream_index INTEGER,
	language VARCHAR(32) NOT NULL,
	raw_language VARCHAR(100),
	language_source VARCHAR(32) NOT NULL,
	title VARCHAR(255),
	codec VARCHAR(64),
	channels INTEGER,
	is_default BOOL NOT NULL,
	is_forced BOOL NOT NULL,
	bound_video_id VARCHAR(64),
	binding_status VARCHAR(24) NOT NULL,
	content_hash VARCHAR(64),
	content_ref_id VARCHAR(32),
	cue_count INTEGER NOT NULL,
	parse_status VARCHAR(24) NOT NULL,
	index_state VARCHAR(24) NOT NULL,
	index_bytes BIGINT NOT NULL,
	last_used BIGINT NOT NULL,
	error TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(file_id) REFERENCES files (id),
	FOREIGN KEY(bound_video_id) REFERENCES files (id),
	FOREIGN KEY(content_ref_id) REFERENCES tracks (id)
);

CREATE INDEX ix_media_language ON tracks (kind, language, file_id);

CREATE INDEX ix_tracks_content_hash ON tracks (content_hash);

CREATE INDEX ix_tracks_content_ref_id ON tracks (content_ref_id);

CREATE INDEX ix_tracks_file_id ON tracks (file_id);

CREATE INDEX ix_tracks_index_state ON tracks (index_state);

CREATE UNIQUE INDEX uq_media_track ON tracks (file_id, stream_key);

CREATE TABLE work_names (
	id VARCHAR(32) NOT NULL,
	work_id VARCHAR(32) NOT NULL,
	name VARCHAR(255) NOT NULL,
	normalized VARCHAR(255) NOT NULL,
	language VARCHAR(32) NOT NULL,
	kind VARCHAR(16) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(work_id) REFERENCES works (id)
);

CREATE INDEX ix_work_names_normalized ON work_names (normalized);

CREATE INDEX ix_work_names_work_id ON work_names (work_id);

CREATE UNIQUE INDEX uq_work_name ON work_names (work_id, normalized, language);

CREATE TABLE episode_files (
	episode_id VARCHAR(32) NOT NULL,
	file_id VARCHAR(64) NOT NULL,
	match_status VARCHAR(24) NOT NULL,
	confidence FLOAT NOT NULL,
	evidence MEDIUMTEXT NOT NULL,
	PRIMARY KEY (episode_id, file_id),
	FOREIGN KEY(episode_id) REFERENCES episodes (id),
	FOREIGN KEY(file_id) REFERENCES files (id)
);

CREATE INDEX ix_episode_files_file_id ON episode_files (file_id);

CREATE TABLE subtitle_cues (
	track_id VARCHAR(32) NOT NULL,
	ordinal INTEGER NOT NULL,
	start_ms INTEGER NOT NULL,
	end_ms INTEGER NOT NULL,
	text TEXT NOT NULL,
	search_text MEDIUMTEXT,
	search_end_ms INTEGER,
	search_document MEDIUMTEXT,
	PRIMARY KEY (track_id, ordinal),
	CONSTRAINT ck_media_cue_time CHECK (start_ms>=0 AND end_ms>start_ms),
	FOREIGN KEY(track_id) REFERENCES tracks (id)
);

CREATE FULLTEXT INDEX ft_media_document ON subtitle_cues (search_document);
