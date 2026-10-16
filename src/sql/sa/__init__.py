"""SQLAlchemy layer: one engine factory and one catalog reader for every supported database.

Used for reading the catalog (schemas, tables, views, columns, keys, constraints, indexes,
comments, enum values) across ALL schemas a connection can see. Question answering still
runs through the existing connectors; this layer never executes anything but catalog reads.

Origin: new (no previous file)
"""
