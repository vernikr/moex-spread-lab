"""Explicit, integrity-bound H1 activation; no source archival or deletion."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pyarrow.parquet as pq

from .data_lake import BarSeries, DataLakeError, ParquetBarStore
from .h1_migration import build_h1_plan, verify_h1_stage
from .w0 import canonical_bytes, sha256_bytes, sha256_file

SCHEMA = "h1-sealed-read.v1"


def _signature(path: Path):
    st = path.stat()
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _new_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


class SealedH1Reader:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "data-lake/h1-seal.json"
        self.marker = self.root / "data-lake/h1-seal.enabled.json"
        self._snapshot = None
        self._payload = None
        self._cache = {}
        self._verified_sigs = set()

    def payload(self):
        exists = [os.path.lexists(p) for p in (self.path, self.marker)]
        if not any(exists):
            self._snapshot = self._payload = None
            self._cache.clear()
            self._verified_sigs.clear()
            return None
        if not all(exists) or self.path.is_symlink() or self.marker.is_symlink():
            raise DataLakeError("H1 seal/activation marker missing or unsafe; refusing fallback")
        try:
            signature = (_signature(self.path), _signature(self.marker))
            if signature == self._snapshot:
                return self._payload
            value = json.loads(self.path.read_text())
            marker = json.loads(self.marker.read_text())
            if (value.get("schema_version") != SCHEMA or value.get("root") != str(self.root)
                    or marker.get("schema_version") != SCHEMA
                    or sha256_bytes(canonical_bytes(value)) != marker.get("seal_sha256")):
                raise DataLakeError("H1 seal integrity mismatch; refusing fallback")
            plan = value["plan"]
            expected = {e["legacy_path"] for e in plan["files"]}
            if set(value["records"]) != expected:
                raise DataLakeError("H1 seal membership mismatch")
            for relative, record in value["records"].items():
                p = Path(relative)
                if (p.is_absolute() or ".." in p.parts or len(p.parts) != 4
                        or p.parts[:2] != ("data", "w1") or p.parts[2] not in ("candles", "candles-screen")
                        or record["legacy_path"] != relative or record["timeframe"] != "1h"):
                    raise DataLakeError("unsafe H1 seal source identity")
            if any(p not in expected for p in plan["screen_selection"].values()):
                raise DataLakeError("invalid H1 seal selection")
            self._snapshot, self._payload = signature, value
            self._cache.clear()
            self._verified_sigs.clear()
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DataLakeError(f"cannot read H1 seal: {exc}") from exc

    def select(self, secid: str) -> Path:
        value = self.payload()
        screened = self.root / "data/w1/candles-screen" / f"{secid}_1h.json"
        intake = self.root / "data/w1/candles" / f"{secid}_1h.json"
        if value is None:
            return screened if screened.is_file() else intake
        relative = value["plan"]["screen_selection"].get(secid)
        if relative is None:
            return screened if screened.is_file() else intake  # read() rejects any unsealed input.
        chosen = self.root / relative
        if chosen != screened and os.path.lexists(screened):
            raise DataLakeError("new preferred screened H1 source requires a new seal")
        return chosen

    def read(self, legacy_path: Path, *, columns=None):
        value = self.payload()
        if value is None:
            return None
        try:
            relative = legacy_path.absolute().relative_to(self.root).as_posix()
            record = value["records"].get(relative)
            if record is None:
                raise DataLakeError(f"H1 input is not sealed: {relative}")
            # Never follow a replacement source symlink, even when JSON is optional.
            for p in (legacy_path, *legacy_path.parents):
                if p == self.root: break
                if p.is_symlink(): raise DataLakeError("symlink H1 source is not allowed")
            self.select(record["secid"])  # Reject a newly introduced preferred source.
            bars = self.root / record["bars_path"]
            sources = self.root / record["sources_path"]
            for path in (bars, sources):
                if not path.resolve().is_relative_to(self.root / "data-lake") or path.is_symlink():
                    raise DataLakeError("sealed tables must remain inside data-lake")
            legacy_signature = _signature(legacy_path) if os.path.lexists(legacy_path) else None
            bars_sig = _signature(bars)
            sources_sig = _signature(sources)
            key = (relative, tuple(columns) if columns is not None else None,
                   bars_sig, sources_sig, legacy_signature, self._snapshot)
            if key in self._cache:
                return self._cache[key]
            verify_key = (relative, bars_sig, sources_sig, legacy_signature, self._snapshot)
            if verify_key not in self._verified_sigs:
                if legacy_signature is not None and sha256_file(legacy_path) != record["legacy_sha256"]:
                    raise DataLakeError("retained H1 JSON changed after sealing; refusing stale Parquet")
                if (sha256_file(bars) != record["parquet_sha256"]
                        or sha256_file(sources) != record["sources_sha256"]):
                    raise DataLakeError("sealed H1 table hash mismatch; refusing fallback")
                self._verified_sigs.add(verify_key)
            table = pq.ParquetFile(bars).read(columns=list(columns) if columns is not None else None)
            rows = table.to_pylist()
            semantics = record.get("row_semantics", {})
            if semantics.get("price_proxy_kind") is not None:
                for row in rows: row["price_proxy_kind"] = semantics["price_proxy_kind"]
            series = BarSeries(schema_version="iss-candles.v1", secid=record["secid"], interval=60,
                               timeframe="1h", rows=tuple(rows), source_path=str(legacy_path),
                               source_sha256=record["legacy_sha256"], backend="sealed_parquet",
                               cache_key=key, metadata=record)
            self._cache[key] = series
            return series
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DataLakeError(f"sealed H1 read failed: {exc}") from exc


def activate_h1(root: Path, plan_path: Path, candidate: Path) -> dict:
    root = root.resolve()
    reader = SealedH1Reader(root)
    if any(os.path.lexists(p) for p in (reader.path, reader.marker)):
        raise DataLakeError("H1 seal already exists; no implicit replacement")
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root / "data-lake"):
        raise DataLakeError("production H1 candidate must be inside data-lake")
    verify_h1_stage(root, plan_path, candidate)
    plan = json.loads(plan_path.read_text())
    records = {}
    for entry in plan["files"]:
        record = ParquetBarStore(root, candidate / entry["source_group"]).record_for_legacy_path(root / entry["legacy_path"])
        if record is None: raise DataLakeError("missing candidate record")
        records[entry["legacy_path"]] = record
    value = {"schema_version": SCHEMA, "root": str(root), "plan": plan, "records": records,
             "source_deletion_allowed": False}
    verify_h1_stage(root, plan_path, candidate)
    # Marker first: interrupted activation fails closed instead of silently falling back.
    _new_json(reader.marker, {"schema_version": SCHEMA, "seal_sha256": sha256_bytes(canonical_bytes(value))})
    _new_json(reader.path, value)
    return verify_sealed_h1(root)


def verify_sealed_h1(root: Path) -> dict:
    reader = SealedH1Reader(root)
    value = reader.payload()
    if value is None: raise DataLakeError("no H1 seal is active")
    retained = 0
    for relative in value["records"]:
        path = reader.root / relative
        reader.read(path, columns=("begin", "close", "volume"))
        retained += int(path.is_file())
    return {"status": "sealed_verified", "dataset_count": len(value["records"]),
            "retained_json_count": retained, "source_fingerprint": value["plan"]["source_fingerprint"],
            "source_deletion_allowed": False}


def deactivate_h1(root: Path) -> dict:
    reader = SealedH1Reader(root)
    value = reader.payload()
    if value is None: raise DataLakeError("no H1 seal is active")
    if build_h1_plan(reader.root) != value["plan"]:
        raise DataLakeError("rollback requires all original JSON and exact selection restored")
    # Preserve every activation's controls, including repeated rollback rehearsals.
    history = reader.root / "data-lake/h1-seal-history" / uuid.uuid4().hex
    history.mkdir(parents=True, exist_ok=False)
    for path in (reader.path, reader.marker):
        os.link(path, history / path.name)
    for path in (reader.path, reader.marker):
        path.unlink()
    return {"status": "deactivated", "restored_json_count": len(value["records"]),
            "evidence_dir": str(history)}


def reject_sealed_table_write(root: Path, bars_path: Path) -> None:
    value = SealedH1Reader(root).payload()
    if value and any((root / r['bars_path']).resolve() == bars_path.resolve() for r in value['records'].values()):
        raise DataLakeError('cannot rewrite an active sealed table; restore JSON and deactivate first')


def verify_archived_record(store: ParquetBarStore, legacy_path: Path) -> dict:
    """Verify retained pilot/candidate mirrors against sealed semantics after archival."""
    import duckdb
    reader = SealedH1Reader(store.root)
    value = reader.payload()
    relative = legacy_path.absolute().relative_to(store.root).as_posix()
    if value is None or relative not in value['records']:
        raise DataLakeError('missing legacy source without an exact verified H1 seal')
    sealed = value['records'][relative]
    reader.read(legacy_path, columns=('begin',))  # Validate seal and both authoritative files.
    record = store.record_for_legacy_path(legacy_path)
    if record is None:
        raise DataLakeError('missing exact-path lake record')
    for key in ('dataset_id','secid','timeframe','interval','row_count','first_begin','last_begin',
                'legacy_sha256','legacy_metadata','row_semantics','source_page_count'):
        if record.get(key) != sealed.get(key):
            raise DataLakeError(f'archived dataset metadata mismatch: {key}')
    for path_key, hash_key in (('bars_path','parquet_sha256'),('sources_path','sources_sha256')):
        path = store._record_path(record[path_key])
        if sha256_file(path) != record[hash_key]:
            raise DataLakeError('archived mirror file hash mismatch')
        if not pq.ParquetFile(path).read().equals(pq.ParquetFile(store.root / sealed[path_key]).read()):
            raise DataLakeError('archived mirror differs from sealed semantics')
    with duckdb.connect(str(store.catalog_path), read_only=True) as connection:
        row = connection.execute('SELECT row_count, legacy_sha256, parquet_sha256, sources_sha256 FROM bar_datasets WHERE dataset_id = ?', [record['dataset_id']]).fetchone()
    if row != (record['row_count'],record['legacy_sha256'],record['parquet_sha256'],record['sources_sha256']):
        raise DataLakeError('archived mirror catalog mismatch')
    return {'status':'verified','verification_basis':'sealed_h1','dataset_id':record['dataset_id'],
            'row_count':record['row_count'],'legacy_sha256':record['legacy_sha256']}
