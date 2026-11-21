import json
from pathlib import Path
from unittest.mock import patch

import pytest

from test_run_artifacts import make_baseline
from moex_spread_lab.cli import main
from moex_spread_lab.accounting.run_artifact_import import import_baseline_artifact, _publish_new_directory
from moex_spread_lab.accounting.run_artifacts import RunArtifactError, load_run_artifact, verify_run_artifact
from moex_spread_lab.core.w0 import sha256_file


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / 'baseline.json'
    path.write_text(json.dumps(make_baseline()))
    return path


def test_import_roundtrip_and_lineage(legacy):
    raw = legacy.read_bytes()
    output = legacy.parent / 'run'
    result = import_baseline_artifact(legacy, output, expected_sha256=sha256_file(legacy))
    assert legacy.read_bytes() == raw
    assert load_run_artifact(output)[0] == json.loads(raw)
    manifest = verify_run_artifact(output)['manifest']
    assert manifest['legacy_import']['legacy_sha256'] == sha256_file(legacy)
    assert manifest['retention']['detail_ttl_days'] == 30
    assert manifest['retention']['pinned'] is False
    assert result['consumer_fingerprints'] == manifest['legacy_import']['consumer_fingerprints']
    assert len(result['consumer_fingerprints']) == 2
    assert set(p.name for p in output.iterdir()) == {'summary.json', 'manifest.json', 'daily.parquet', 'trades.parquet'}
    manifest['legacy_import']['legacy_sha256'] = '0'*64
    (output/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(RunArtifactError, match='identity'):
        verify_run_artifact(output)


@pytest.mark.parametrize('fault', ['schema','nan','duplicate','sparse','missing','empty','numeric_coercion'])
def test_invalid_or_lossy_reports_fail_closed(legacy, fault):
    report = json.loads(legacy.read_text())
    if fault == 'schema': report['schema_version'] = 'calendar-study.v1'
    if fault == 'nan': report['summary']['final_X'] = float('nan')
    if fault == 'sparse': report['daily'][-1]['only_in_last_row'] = 'must not disappear'
    if fault == 'numeric_coercion':
        report['daily'][0]['new_number'] = 1
        for row in report['daily'][1:]: row['new_number'] = 1.5
    if fault == 'missing': del report['trades']
    if fault == 'empty': report['daily'] = []
    text = json.dumps(report)
    if fault == 'duplicate': text = text[:-1] + ',"schema_version":"analytical-baseline.v1"}'
    legacy.write_text(text)
    output = legacy.parent / 'run'
    with pytest.raises(RunArtifactError): import_baseline_artifact(legacy, output)
    assert not output.exists()
    assert not list(legacy.parent.glob('.run.import-*'))
    assert legacy.read_text() == text


def test_expected_hash_and_pin_and_cli(legacy):
    output = legacy.parent / 'run'
    with pytest.raises(RunArtifactError, match='SHA-256'):
        import_baseline_artifact(legacy, output, expected_sha256='0'*64)
    with patch('sys.argv',['moex-spread-lab','run-artifact-import','--legacy',str(legacy),'--output-dir',str(output),'--pin']):
        assert main() == 0
    assert verify_run_artifact(output)['manifest']['retention']['pinned'] is True
    with patch('sys.argv',['moex-spread-lab','run-artifact-import','--legacy',str(legacy),'--output-dir',str(output)]):
        assert main() == 2


@pytest.mark.parametrize('kind',['directory','file','symlink'])
def test_existing_target_never_replaced(legacy,kind):
    output = legacy.parent / 'run'
    if kind == 'directory': output.mkdir()
    if kind == 'file': output.write_text('preserve')
    if kind == 'symlink': output.symlink_to(legacy.parent / 'missing')
    with pytest.raises(RunArtifactError): import_baseline_artifact(legacy,output)


def test_source_mutation_and_publication_race(legacy):
    import moex_spread_lab.accounting.run_artifact_import as module
    original = module._consumer_checks
    output = legacy.parent/'run'
    def mutate(*args):
        value = original(*args)
        legacy.write_text('{}')
        return value
    with patch.object(module,'_consumer_checks',side_effect=mutate):
        with pytest.raises(RunArtifactError,match='changed'):
            import_baseline_artifact(legacy,output)
    assert not output.exists()
    source = legacy.parent/'staged';source.mkdir();(source/'file').write_text('keep')
    output.mkdir()
    with pytest.raises(FileExistsError): _publish_new_directory(source,output)
    assert not list(output.iterdir()) and (source/'file').read_text()=='keep'


def test_consumer_mismatch_never_publishes(legacy):
    with patch('moex_spread_lab.accounting.run_artifact_import._consumer_checks',side_effect=[{'x':'old'},{'x':'new'}]):
        with pytest.raises(RunArtifactError,match='consumer results differ'):
            import_baseline_artifact(legacy,legacy.parent/'run')
    assert not (legacy.parent/'run').exists()
