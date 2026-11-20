import json
from unittest.mock import patch

import pytest
from test_h1_seal import prepared
from moex_spread_lab.h1_archive import execute
from moex_spread_lab.h1_seal import activate_h1, SealedH1Reader
from moex_spread_lab.data_lake import ParquetBarStore, DataLakeError
from moex_spread_lab.w0 import canonical_bytes, sha256_bytes


@pytest.fixture
def archive_case(prepared,tmp_path):
    root,intake,screen,plan,candidate=prepared
    ParquetBarStore(root).import_legacy(intake)
    activate_h1(root,plan,candidate)
    seal=SealedH1Reader(root).payload()
    evidence=root/'evidence.json'
    evidence.write_text(json.dumps({'source_fingerprint':seal['plan']['source_fingerprint'],
        'seal_sha256':sha256_bytes(canonical_bytes(seal)),
        'checks':{k:'passed' for k in ('regression','performance','ui_smoke')}}))
    archive=root/'archives/h1.tar.zst';ap=root/'archive-plan.json'
    return root,intake,screen,candidate,evidence,archive,ap


def _prepare(case):
    root,intake,screen,candidate,evidence,archive,ap=case
    execute(root,ap,'dry-run',archive=archive,evidence=evidence)
    # Fixture roots are not git checkouts; tiny synthetic rows cannot amortize DuckDB overhead.
    with patch('moex_spread_lab.data_archive_candidates.subprocess.check_output',return_value='a'*40), patch('moex_spread_lab.h1_archive._gain',return_value={'net_gain_fraction':0.9}):
        return execute(root,ap,'archive-only')


def test_archive_restore_then_explicit_apply_and_missing_json_maintenance(archive_case):
    root,intake,screen,candidate,evidence,archive,ap=archive_case
    from moex_spread_lab.proof_pool import _estimate_weight
    target={'universe':'pair_screen','pair':'FAR/NEAR'}
    expected_weight=_estimate_weight(root,target,'1h')
    receipt=_prepare(archive_case)
    assert receipt['restore_verified_files']==2
    assert intake.exists() and screen.exists()
    with patch('moex_spread_lab.h1_archive._gain',return_value={'net_gain_fraction':0.9}):
        result=execute(root,ap,'apply')
    assert result['status']=='applied' and len(result['deleted'])==2
    assert not intake.exists() and not screen.exists()
    assert _estimate_weight(root,target,'1h')==expected_weight
    assert ParquetBarStore(root).verify_all()['datasets'][0]['verification_basis']=='sealed_h1'
    assert ParquetBarStore(root,candidate/'candles-screen').verify_all()['dataset_count']==1
    assert SealedH1Reader(root).read(screen).rows[0]['close']==20.
    with pytest.raises(DataLakeError): ParquetBarStore(root,candidate/'candles-screen').compact(force=True)
    # An unrelated missing dataset never acquires a substitute just because a seal exists.
    with pytest.raises(DataLakeError): ParquetBarStore(root).verify_legacy(root/'data/w1/candles/OTHER_1h.json')


@pytest.mark.parametrize('fault',['source','archive','evidence','seal'])
def test_stale_or_corrupt_inputs_delete_nothing(archive_case,fault):
    root,intake,screen,candidate,evidence,archive,ap=archive_case
    _prepare(archive_case)
    path={'source':screen,'archive':archive,'evidence':evidence,'seal':root/'data-lake/h1-seal.json'}[fault]
    path.write_bytes(b'{}')
    with pytest.raises((DataLakeError,ValueError)):
        execute(root,ap,'apply')
    assert intake.exists() and screen.exists()


def test_gain_gate_refuses_small_source_and_existing_destination(archive_case):
    root,intake,screen,candidate,evidence,archive,ap=archive_case
    execute(root,ap,'dry-run',archive=archive,evidence=evidence)
    with patch('moex_spread_lab.data_archive_candidates.subprocess.check_output',return_value='a'*40):
        with pytest.raises(DataLakeError,match='80%'): execute(root,ap,'archive-only')
        with pytest.raises(ValueError,match='exists'): execute(root,ap,'archive-only')
    assert intake.exists() and screen.exists()
    assert not archive.with_name(archive.name+'.verified.json').exists()


def test_failed_evidence_and_sealed_refresh_refused(archive_case):
    root,intake,screen,candidate,evidence,archive,ap=archive_case
    value=json.loads(evidence.read_text());value['checks']['performance']='failed';evidence.write_text(json.dumps(value))
    with pytest.raises(DataLakeError,match='evidence'): execute(root,ap,'dry-run',archive=archive,evidence=evidence)
    from moex_spread_lab.data_refresh import refresh_candle_datasets, RefreshTarget, DataRefreshError
    with pytest.raises(DataRefreshError,match='deactivate'):
        refresh_candle_datasets(root=root,targets=[RefreshTarget('NEAR',60)],till_date='2026-11-21')
