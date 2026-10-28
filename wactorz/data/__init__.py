# SPDX-License-Identifier: Apache-2.0
"""Where the model loop's data lives, and how it is read back.

Readings from streams, shadow pairs and serving logs are Parquet files under
the state directory, one directory per source and hour, written whole and
never appended to; main's SQLite keeps what is small and transactional. The
decision is ``docs/design/data-storage.md``.

`files` knows the layout and needs nothing but the standard library; `parquet`
writes and reads the files and needs pyarrow, the ``data`` extra, so it is
imported where it is used rather than here.
"""

from .files import DATA_DIRNAME, DataError, data_root, event_time_of, files_for, hour_dir

__all__ = ["DATA_DIRNAME", "DataError", "data_root", "event_time_of", "files_for", "hour_dir"]
