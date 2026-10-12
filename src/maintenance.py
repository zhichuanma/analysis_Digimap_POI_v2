"""Executed review notebooks, isolated-copy portability and recorded tests."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from src.pipeline import artifact_hashes, source_fingerprints, verify_run
from src.validation import fingerprint, read_json, write_json


def check_directory(run_dir, name):
    out = Path(run_dir) / 'checks' / (name + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6])
    out.mkdir(parents=True, exist_ok=False)
    return out


def notebook(run_dir, *, code_root, data_root):
    import nbformat
    from nbclient import NotebookClient
    from jupyter_client import KernelManager
    verify_run(run_dir, code_root=code_root, data_root=data_root, require_success=True)
    out = check_directory(run_dir, 'notebook')
    start = time.monotonic()
    result = {'status': 'running', 'run_id': Path(run_dir).name}
    template = Path(code_root) / 'notebooks/01_review.ipynb'
    before = fingerprint(template)
    nb = nbformat.read(template, as_version=4)
    parameters = '\n'.join(['from pathlib import Path', f'PROJECT_ROOT = Path({str(Path(code_root).resolve())!r})',
        f'DATA_ROOT = Path({str(Path(data_root).resolve())!r})', f'RUN_ID = {Path(run_dir).name!r}',
        f'RUN_DIR = Path({str(Path(run_dir).resolve())!r})'])
    nb.cells[0] = nbformat.v4.new_code_cell(parameters, metadata={'tags': ['parameters']})
    km = KernelManager(kernel_name='python3')
    km.kernel_spec.argv = [sys.executable, '-B', '-m', 'ipykernel_launcher', '-f', '{connection_file}']
    try:
        NotebookClient(nb, timeout=1200, km=km, resources={'metadata': {'path': str(out)}}).execute()
        if fingerprint(template) != before:
            raise ValueError('notebook template changed')
        verification = verify_run(run_dir, code_root=code_root, data_root=data_root, require_success=True)
        result.update(status='passed', verification=verification, template_fingerprint=before,
                      code_cells=sum(c.cell_type == 'code' for c in nb.cells))
    except Exception as error:
        result.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        nbformat.write(nb, out / '01_review.executed.ipynb')
        result.update(elapsed_seconds=time.monotonic() - start, executed_fingerprint=fingerprint(out / '01_review.executed.ipynb'))
        write_json(out / 'notebook_check.json', result)
    return result | {'output_dir': str(out)}


def portability(run_dir, *, code_root, data_root, sample_size=24):
    if type(sample_size) is not int or not 1 <= sample_size <= 24:
        raise ValueError('portability allows 1..24 stations')
    verify_run(run_dir, code_root=code_root, data_root=data_root, require_success=True)
    code, data = Path(code_root).resolve(), Path(data_root).resolve()
    snap = read_json(Path(run_dir) / 'snapshot.json')
    out = check_directory(run_dir, 'portability')
    start = time.monotonic()
    scratch = Path(tempfile.mkdtemp(prefix='portability-copy-', dir=data / 'outputs'))
    new_code, new_data, outside = scratch / 'code', scratch / 'data', scratch / 'external-cwd'
    result = {'status': 'running', 'sample_stations': sample_size, 'scratch_directory': str(scratch),
        'audit_scope': 'Python open/listdir/scandir/chdir/sqlite-connect events; not OS isolation or native-library I/O interception',
        'dependency_environment': 'existing pinned interpreter/packages; no old code, source-data or outputs are permitted'}
    try:
        new_code.mkdir()
        new_data.mkdir()
        outside.mkdir()
        copied = {}
        for name, expected in snap['code'].items():
            source, target = code / name, new_code / name
            if source.is_symlink():
                raise ValueError(f'symlink source: {source}')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if fingerprint(target) != expected:
                raise ValueError('copied code mismatch')
            copied['code/' + name] = expected
        for name, expected in snap['inputs'].items():
            source, target = data / name, new_data / name
            if source.is_symlink() or any(p.is_symlink() for p in source.parents if p != data and p.is_relative_to(data)):
                raise ValueError(f'symlink input: {source}')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if fingerprint(target) != expected or fingerprint(source) != expected:
                raise ValueError('copied raw input mismatch')
            copied['data/' + name] = expected
        if any(p.is_symlink() for p in scratch.rglob('*')) or (new_data / 'outputs').exists():
            raise ValueError('copy contains links or prior outputs')
        prohibited = [str(code), str(data / 'data'), str(data / 'cities'), str(data / 'outputs')]
        # The independent scratch directory is inside outputs; exempt only it.
        audit = outside / 'audit.json'
        launcher = outside / 'launch.py'
        launcher.write_text('''import atexit, json, os, pathlib, runpy, sys
blocked = BLOCKED
scratch = pathlib.Path(SCRATCH).resolve()
report = {"events_checked": 0, "forbidden_accesses": []}
auditing = False
def hook(event, args):
    global auditing
    if auditing or event not in ("open", "os.listdir", "os.scandir", "os.chdir", "sqlite3.connect"):
        return
    if not args or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    auditing = True
    try:
        raw = os.fsdecode(args[0])
        if raw in (":memory:", ""):
            return
        if raw.startswith("file:"):
            from urllib.parse import unquote, urlsplit
            raw = unquote(urlsplit(raw).path)
        path = pathlib.Path(raw).resolve()
        report["events_checked"] += 1
        if not path.is_relative_to(scratch) and any(path.is_relative_to(pathlib.Path(p)) for p in blocked):
            report["forbidden_accesses"].append({"event": event, "path": str(path)})
            raise PermissionError("old workspace access: " + str(path))
    finally:
        auditing = False
def save():
    pathlib.Path(AUDIT).write_text(json.dumps(report, indent=2))
atexit.register(save)
sys.addaudithook(hook)
sys.path.insert(0, CODE)
sys.argv = ARGV
runpy.run_path(sys.argv[0], run_name="__main__")
'''.replace('BLOCKED', repr(prohibited)).replace('SCRATCH', repr(str(scratch))).replace('AUDIT', repr(str(audit))))
        options = snap['options']
        argv = [str(new_code / 'run.py'), '--config', str(Path(snap['config']['config_path']).relative_to(code)),
                '--study-config', str(Path(snap['config']['study_config_path']).relative_to(code)),
                '--run-id', 'portable', '--sample-stations', str(sample_size), '--seed', str(options['seed']),
                '--primary-radius', str(options['primary_radius']), '--threads', str(options['threads']), '--memory-limit', options['memory_limit']]
        launcher.write_text(launcher.read_text().replace('CODE', repr(str(new_code))).replace('ARGV', repr(argv)))
        env = dict(os.environ, DIGIMAP_DATA_ROOT=str(new_data), PROJ_NETWORK='OFF', PYTHONDONTWRITEBYTECODE='1',
                   TMPDIR=str(outside), MPLCONFIGDIR=str(outside / 'mpl'))
        command = [sys.executable, '-B', str(launcher)]
        result.update(command=command, child_argv=argv, external_cwd=str(outside), copied_files=copied)
        with (out / 'execution.log').open('x') as log:
            process = subprocess.run(command, cwd=outside, env=env, stdout=log, stderr=subprocess.STDOUT)
        result['returncode'] = process.returncode
        if audit.exists():
            result['access_audit'] = read_json(audit)
        child = new_data / Path(snap['config']['paths']['output_dir']).relative_to(data) / 'portable'
        for name in ('run.json', 'snapshot.json', 'report/main/report.json', 'report/main/SUMMARY.md',
                     'report/main/validation/independent_validation.json', 'report/main/review/review_report.json'):
            if (child / name).is_file():
                target = out / 'child_evidence' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(child / name, target)
        if process.returncode != 0 or not audit.exists() or result['access_audit']['forbidden_accesses']:
            raise ValueError('portable child failed or accessed old workspace; see execution.log')
        result['child_verification'] = verify_run(child, code_root=new_code, data_root=new_data, require_success=True)
        if source_fingerprints(code, data, snap['study']) != snap['inputs']:
            raise ValueError('original inputs changed during portability check')
        result.update(status='passed', original_inputs_unchanged=True, copied_symlinks=0,
                      child_status=read_json(child / 'run.json')['status'])
    except Exception as error:
        result.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        shutil.rmtree(scratch)
        result.update(copy_cleaned=not scratch.exists(), elapsed_seconds=time.monotonic() - start)
        write_json(out / 'portability_check.json', result)
    return result | {'output_dir': str(out)}


def run_tests(*, code_root, data_root, run_id=None, pattern='test_*.py'):
    import resource
    import unittest
    from src.pipeline import code_fingerprints
    identifier = run_id or 'tests-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    if not __import__('re').fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', identifier):
        raise ValueError('invalid test run-id')
    out = Path(data_root) / 'outputs' / identifier
    out.mkdir(parents=True, exist_ok=False)
    (out / 'tmp').mkdir()
    tempfile.tempdir = str(out / 'tmp')
    os.environ['TMPDIR'] = tempfile.tempdir
    before = code_fingerprints(code_root)
    test_hashes = {p.name: fingerprint(p) for p in sorted((Path(code_root) / 'tests').glob('*.py'))}
    start = time.monotonic()
    suite = unittest.TestLoader().discover(str(Path(code_root) / 'tests'), pattern=pattern)
    with (out / 'tests.log').open('x') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    unchanged = code_fingerprints(code_root) == before and test_hashes == {p.name: fingerprint(p) for p in sorted((Path(code_root) / 'tests').glob('*.py'))}
    report = dict(status='passed' if result.wasSuccessful() and unchanged and result.testsRun else 'failed', tests=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped), elapsed_seconds=time.monotonic() - start,
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, code_unchanged=unchanged,
        code_fingerprints=before, test_fingerprints=test_hashes, output_dir=str(out))
    write_json(out / 'tests.json', report)
    return report
