"""The blob store (STORE.md): large, immutable, rarely read data kept on a Seafile library under its SHA-256.

The command is ``seafile-blob`` (``sfblob``, ``git-seafile-blob``, ``seafile blob``).  This package holds the
pieces; ``cli`` is the command line.  First release: ``put``, ``get``, ``path``, ``cat``, ``exists``, ``stat``,
the cache, references (full and short) and the record file.  ``gc``, pins and ``offload`` come after their review.
"""
