from pathlib import Path
import pytest
from faust.stores import rocksdb
from faust.stores.rocksdb import RocksDBOptions


MiB = 1024 ** 2


def read_options_file(db_path):
    """Return the flattened key/value pairs of the latest RocksDB OPTIONS file."""
    options_file = sorted(Path(db_path).glob('OPTIONS-*'))[-1]
    values = {}
    for line in options_file.read_text().splitlines():
        key, sep, value = line.strip().partition('=')
        if sep:
            values[key] = value
    return values


@pytest.mark.skipif(
    rocksdb.rocksdict is None, reason='rocksdict not installed')
class test_RocksDBOptions__rocksdict:
    """Open real RocksDB databases (no mocks)."""

    @pytest.fixture(autouse=True)
    def _close_dbs(self):
        self.dbs = []
        yield
        for db in self.dbs:
            db.close()

    def options(self, **kwargs):
        return RocksDBOptions(use_rocksdict=True, **kwargs)

    def open(self, path, **kwargs):
        db = self.options(**kwargs).open(path)
        self.dbs.append(db)
        return db

    def close(self, db):
        db.close()
        self.dbs.remove(db)

    def cache_capacity(self, db):
        return db.property_int_value('rocksdb.block-cache-capacity')

    def test_open__new_db(self, *, tmp_path):
        path = tmp_path / 'new.db'
        db = self.open(
            path,
            block_cache_size=64 * MiB,
            bloom_filter_size=10,
            write_buffer_size=32 * MiB,
            max_write_buffer_number=2,
        )

        assert self.cache_capacity(db) == 64 * MiB
        values = read_options_file(path)
        assert values['filter_policy'] == 'bloomfilter'
        assert values['write_buffer_size'] == str(32 * MiB)
        assert values['max_write_buffer_number'] == '2'

    def test_open__existing_db_keeps_options_across_restarts(
            self, *, tmp_path):
        path = tmp_path / 'db.db'
        config = dict(block_cache_size=64 * MiB, bloom_filter_size=10)
        db = self.open(path, **config)
        db.put(b'key', b'value')
        self.close(db)

        db = self.open(path, **config)

        # rocksdict falls back to an 8MiB cache and no bloom filter
        # for existing databases unless the options are passed explicitly.
        assert self.cache_capacity(db) == 64 * MiB
        assert read_options_file(path)['filter_policy'] == 'bloomfilter'
        assert db.get(b'key') == b'value'

    def test_open__existing_db_applies_changed_options(self, *, tmp_path):
        path = tmp_path / 'db.db'
        db = self.open(
            path,
            block_cache_size=64 * MiB,
            bloom_filter_size=10,
            write_buffer_size=32 * MiB,
            max_write_buffer_number=2,
        )
        for i in range(1000):
            db.put(f'key{i}'.encode(), b'value')
        self.close(db)

        db = self.open(
            path,
            block_cache_size=256 * MiB,
            bloom_filter_size=5,
            write_buffer_size=16 * MiB,
            max_write_buffer_number=3,
        )

        assert self.cache_capacity(db) == 256 * MiB
        values = read_options_file(path)
        assert values['filter_policy'] == 'bloomfilter'
        assert values['write_buffer_size'] == str(16 * MiB)
        assert values['max_write_buffer_number'] == '3'
        assert all(db.get(f'key{i}'.encode()) == b'value' for i in range(1000))

    def test_open__shared_block_cache(self, *, tmp_path):
        cache = rocksdb.rocksdict.Cache(32 * MiB)
        db1 = self.open(tmp_path / '1.db', block_cache=cache)
        db2 = self.open(tmp_path / '2.db', block_cache=cache)
        assert self.cache_capacity(db1) == self.cache_capacity(db2) == 32 * MiB

        for i in range(2000):
            db1.put(f'key{i}'.encode(), b'v' * 512)
        db1.flush()
        for i in range(2000):
            db1.get(f'key{i}'.encode())

        usage = db1.property_int_value('rocksdb.block-cache-usage')
        assert usage > 0
        assert db2.property_int_value('rocksdb.block-cache-usage') == usage

    def test_open__block_cache_is_per_db_by_default(self, *, tmp_path):
        options = self.options(block_cache_size=8 * MiB)
        db1 = options.open(tmp_path / '1.db')
        db2 = options.open(tmp_path / '2.db')
        self.dbs.extend([db1, db2])

        for i in range(2000):
            db1.put(f'key{i}'.encode(), b'v' * 512)
        db1.flush()
        for i in range(2000):
            db1.get(f'key{i}'.encode())

        usage = db1.property_int_value('rocksdb.block-cache-usage')
        assert usage > 100 * 1024
        # an idle db only accounts for a few bytes of bookkeeping
        assert db2.property_int_value('rocksdb.block-cache-usage') < 1024
