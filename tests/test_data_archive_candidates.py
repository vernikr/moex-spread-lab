import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
import zstandard as zstd

from moex_spread_lab.data_archive_candidates import execute, verify_archive, _fingerprint
from moex_spread_lab.data_retention import DataRetentionError
from moex_spread_lab.w0 import canonical_bytes


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '--allow-empty', '-qm', 'test'], cwd=root, check=True)
    files = {
        'analytics/report.json': {'schema_version': 'analytical-baseline.v1'},
        'w1/candles-common/A_1m.json': {'timeframe': '1m'},
        'w1/candles/A_10m.json': {'timeframe': '10m'},
        'w1/candles/A_1h.json': {'timeframe': '1h'},
        'w1/candles-screen/A_1h.json': {'timeframe': '1h'},
        'analytics/research-settings.json': {},
        'analytics/data-inventory-old.json': {'schema_version': 'data-inventory.v1'},
        'analytics/runs/a/summary.json': {'schema_version': 'analytical-baseline.v1'},
        'benchmark/value.json': {},
    }
    for name, body in files.items():
        path = root / 'data' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body))
    return root, tmp_path / 'plan.json', tmp_path / 'archive.tar.zst'


def plan(setup):
    root, path, archive = setup
    return execute(root, path, 'dry-run', archive)


def test_roundtrip_and_protection(setup):
    root, path, archive = setup
    before = {str(p.relative_to(root / 'data')): p.read_bytes() for p in (root / 'data').rglob('*.json')}
    result = plan(setup)
    assert result['file_count'] == 3
    assert not archive.exists()
    assert _fingerprint(result['files']) == result['source_fingerprint']
    verified = execute(root, path, 'archive-only')
    assert all((root / 'data' / p).read_bytes() == body for p, body in before.items())
    verify_archive(archive, verified['manifest'])
    # Restore rehearsal without extracting over working data.
    payload = zstd.ZstdDecompressor().stream_reader(io.BytesIO(archive.read_bytes())).read()
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        assert len(tar.getmembers()) == 4
        for entry in result['files']:
            assert tar.extractfile('data/' + entry['path']).read() == before[entry['path']]
    applied = execute(root, path, 'apply')
    assert len(applied['deleted']) == 3
    for name, body in before.items():
        source = root / 'data' / name
        assert not source.exists() if name in applied['deleted'] else source.read_bytes() == body
    assert json.loads(Path(str(archive) + '.apply.json').read_text())['status'] == 'applied'


@pytest.mark.parametrize('mode', ['archive-only', 'apply'])
def test_stale_last_source_deletes_nothing(setup, mode):
    root, path, archive = setup
    result = plan(setup)
    if mode == 'apply':
        execute(root, path, 'archive-only')
    (root / 'data' / result['files'][-1]['path']).write_text('{"timeframe":"10m","changed":true}')
    with pytest.raises(DataRetentionError):
        execute(root, path, mode)
    assert all((root / 'data' / e['path']).exists() for e in result['files'])


def test_corrupt_archive_deletes_nothing(setup):
    root, path, archive = setup
    result = plan(setup)
    execute(root, path, 'archive-only')
    archive.write_bytes(archive.read_bytes()[:30])
    with pytest.raises(DataRetentionError):
        execute(root, path, 'apply')
    assert all((root / 'data' / e['path']).exists() for e in result['files'])


@pytest.mark.parametrize('target', ['../outside', '/absolute', 'w1/candles/A_1h.json', 'analytics/runs/a/summary.json'])
def test_forged_plan_refused(setup, target):
    root, path, archive = setup
    plan(setup)
    result = json.loads(path.read_text())
    result['files'][0]['path'] = target
    result['files'].sort(key=lambda e: e['path'])
    result['source_fingerprint'] = _fingerprint(result['files'])
    path.write_text(json.dumps(result))
    with pytest.raises(DataRetentionError):
        execute(root, path, 'archive-only')
    assert not archive.exists()


def test_existing_destination_and_symlink(setup):
    root, path, archive = setup
    result = plan(setup)
    archive.write_bytes(b'preserve')
    with pytest.raises(DataRetentionError):
        execute(root, path, 'archive-only')
    assert archive.read_bytes() == b'preserve'
    archive.unlink()
    source = root / 'data' / result['files'][0]['path']
    copy = root / 'copy.json'
    copy.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(copy)
    with pytest.raises(DataRetentionError):
        execute(root, path, 'archive-only')


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'extra', 'hash', 'manifest', 'symlink'])
def test_member_verification(setup, fault):
    root, path, archive = setup
    plan(setup)
    record = execute(root, path, 'archive-only')
    manifest = record['manifest']
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        body = canonical_bytes(manifest) if fault != 'manifest' else b'{}'
        info = tarfile.TarInfo('manifest.json'); info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
        for i, entry in enumerate(manifest['files']):
            if i == 0 and fault == 'missing':
                continue
            body = (root / 'data' / entry['path']).read_bytes()
            if i == 0 and fault == 'hash':
                body = b'x' * len(body)
            info = tarfile.TarInfo('data/' + entry['path']); info.size = len(body)
            if i == 0 and fault == 'symlink':
                info.type = tarfile.SYMTYPE; info.linkname = 'elsewhere'; info.size = 0
            tar.addfile(info, io.BytesIO(body))
            if i == 0 and fault == 'duplicate':
                tar.addfile(info, io.BytesIO(body))
        if fault == 'extra':
            tar.addfile(tarfile.TarInfo('extra'))
    archive.write_bytes(zstd.ZstdCompressor().compress(buf.getvalue()))
    with pytest.raises(DataRetentionError):
        verify_archive(archive, manifest)


def test_no_apply_without_receipt(setup):
    root, path, archive = setup
    result = plan(setup)
    with pytest.raises(FileNotFoundError):
        execute(root, path, 'apply')
    assert all((root / 'data' / e['path']).exists() for e in result['files'])


def test_publication_race_never_overwrites(setup, monkeypatch):
    import moex_spread_lab.data_archive_candidates as module
    root, path, archive = setup
    result = plan(setup)
    original = module.verify_archive
    def racing_verify(staging, manifest):
        original(staging, manifest)
        archive.write_bytes(b'other publisher')
    monkeypatch.setattr(module, 'verify_archive', racing_verify)
    with pytest.raises(FileExistsError):
        execute(root, path, 'archive-only')
    assert archive.read_bytes() == b'other publisher'
    assert all((root / 'data' / e['path']).exists() for e in result['files'])
    assert not list(archive.parent.glob('.archive.tar.zst.*'))


def test_source_changed_during_archive_is_not_published(setup, monkeypatch):
    import moex_spread_lab.data_archive_candidates as module
    root, path, archive = setup
    result = plan(setup)
    original = module.verify_archive
    def change_source(staging, manifest):
        original(staging, manifest)
        (root / 'data' / result['files'][-1]['path']).write_text('{}')
    monkeypatch.setattr(module, 'verify_archive', change_source)
    with pytest.raises(DataRetentionError):
        execute(root, path, 'archive-only')
    assert not archive.exists()
    assert all((root / 'data' / e['path']).exists() for e in result['files'])
