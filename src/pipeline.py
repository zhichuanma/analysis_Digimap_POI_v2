"""Five stages with immutable attempts, complete fingerprints and file verification."""
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import re
import resource
import sys
import time

from src.validation import fingerprint, read_json, write_json


STAGES = ('inspect', 'prepare', 'match', 'summarize', 'report')


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    write_json(tmp, value)
    tmp.replace(path)


def artifact_hashes(directory):
    root = Path(directory)
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'symlink not permitted in artifacts: {path}')
        if path.is_file():
            result[str(path.relative_to(root))] = fingerprint(path)
    return result


def source_fingerprints(code_root, data_root, study):
    root = Path(data_root).resolve()
    result = {}
    for name in ['data/source_manifest.json', *study['required_paths']]:
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f'input escapes data root: {name}')
        result[name] = fingerprint(path)
    return result


def code_fingerprints(code_root, extra_paths=()):
    root = Path(code_root).resolve()
    paths = [root / 'run.py', root / 'requirements.txt', root / 'config.yaml']
    for folder, pattern in (('src', '*.py'), ('config', '*'), ('notebooks', '*.ipynb')):
        paths.extend(p for p in (root / folder).glob(pattern) if p.is_file())
    for name in extra_paths:
        path = Path(name).resolve()
        if not path.is_relative_to(root):
            raise ValueError('selected configuration escapes code root')
        paths.append(path)
    return {str(p.relative_to(root)): fingerprint(p) for p in sorted(set(paths))}


def configuration_files(config):
    return [config[k] for k in ('config_path', 'study_config_path') if k in config]


def dependency_fingerprint(code_root):
    packages = {}
    for line in (Path(code_root) / 'requirements.txt').read_text().splitlines():
        if line and not line.startswith('#'):
            name, expected = line.split('==')
            actual = importlib.metadata.version(name)
            if actual != expected:
                raise ValueError(f'dependency mismatch: {name} expected {expected}, actual {actual}')
            packages[name] = actual
    environment = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get('Name', '').lower().replace('_', '-')
        environment.setdefault(name, distribution.version)
    return {'python': sys.version, 'executable': sys.executable, 'packages': packages,
            'installed_distributions': dict(sorted(environment.items()))}


def seal(directory):
    """Seal a leaf whose own implementation has completed its readback checks."""
    directory = Path(directory)
    reports = [p for p in directory.glob('*.json') if p.name.endswith('_report.json') or p.name in ('input_inspection.json', 'dictionary_validation.json', 'report.json')]
    if not reports:
        raise ValueError(f'no completion report to seal: {directory}')
    for path in reports:
        report = read_json(path)
        if report.get('status') not in ('passed', 'completed'):
            raise ValueError(f'incomplete leaf: {path}')
    target = directory.parent / 'verification.json'
    if target.exists():
        raise FileExistsError(target)
    write_json(target, {'status': 'passed', 'kind': 'stage_completion_and_artifact_integrity',
        'artifacts': {str(Path(directory.name) / k): v for k, v in artifact_hashes(directory).items()}})


def stage_paths(run_dir, data_root):
    root = Path(data_root)
    locations = {'inspection': 'inspect/inputs/main', 'dictionary': 'inspect/dictionary/main',
        'scope': 'prepare/geography/main', 'coverage': 'prepare/coverage/main', 'stations': 'prepare/stations/main',
        'pois': 'prepare/pois/main', 'matches': 'match/main', 'summary': 'summarize/main', 'report': 'report/main'}
    return {k: str((Path(run_dir) / v).relative_to(root)) for k, v in locations.items()}


def execute_stage(stage, *, config, study, run_dir, options, progress):
    from src import data, geography
    root, code = config['data_root'], config['code_root']
    paths = stage_paths(run_dir, root)
    required = study['required_paths']
    if stage == 'inspect':
        sources = data.verify_sources(data_root=root, required_paths=required)
        Path(run_dir / 'inspect').mkdir(exist_ok=True)
        write_json(run_dir / 'inspect/source_verification.json', sources)
        data.inspect_inputs(study['poi_path'], study['station_path'], data_root=root, output_dir=paths['inspection'],
            layer=study['poi_layer'], poi_batch_size=config['poi_batch_size'], station_batch_size=config['station_batch_size'], progress=progress)
        seal(root / paths['inspection'])
        data.inspect_dictionary(study['group_path'], study['category_path'], study['class_path'], study['poi_path'],
            data_root=root, inspection_dir=paths['inspection'], output_dir=paths['dictionary'], layer=study['poi_layer'],
            required_paths=required, batch_size=config['poi_batch_size'], progress=progress)
        seal(root / paths['dictionary'])
    elif stage == 'prepare':
        geography.prepare_pua_scope(**study['geography'], data_root=root, output_dir=paths['scope'],
            inspection_dir=paths['inspection'], coordinate_operation=config['coordinate_operation'], required_paths=required,
            station_batch_size=config['station_batch_size'])
        seal(root / paths['scope'])
        station_options = dict(data_root=root, scope_dir=paths['scope'], inspection_dir=paths['inspection'],
            coordinate_operation=config['coordinate_operation'], required_paths=required, order_review=study['order_review'],
            poi_layer=study['poi_layer'])
        arguments = [study[k] for k in ('station_path', 'lad_path', 'order_path', 'poi_path')]
        data.prepare_station_files(*arguments, output_dir=paths['coverage'], **station_options)
        seal(root / paths['coverage'])
        selected = paths['coverage']
        if options['sample_stations'] is not None:
            data.prepare_station_files(*arguments, output_dir=paths['stations'], sample_size=options['sample_stations'],
                random_seed=options['seed'], **station_options)
            seal(root / paths['stations'])
            selected = paths['stations']
        context = {k: study[k] for k in ('group_path', 'category_path', 'class_path', 'accuracy_path', 'rules_config', 'required_paths')}
        context.update(layer=study['poi_layer'], inspection_dir=paths['inspection'], dictionary_dir=paths['dictionary'],
                       coverage_dir=paths['coverage'], scope_dir=paths['scope'])
        data.prepare(study['poi_path'], station_dir=selected, data_root=root, code_root=code, output_dir=paths['pois'],
            context_options=context, batch_size=config['poi_batch_size'], include_ev_charging=config['include_ev_charging'], progress=progress)
        seal(root / paths['pois'])
    elif stage == 'match':
        data.match_files(paths['pois'], data_root=root, code_root=code, output_dir=paths['matches'],
                         station_batch_size=config['station_batch_size'], progress=progress)
        seal(root / paths['matches'])
    elif stage == 'summarize':
        data.summarize_files(paths['matches'], data_root=root, code_root=code, output_dir=paths['summary'],
                            memory_limit=options['memory_limit'], threads=options['threads'], progress=progress)
        seal(root / paths['summary'])
    elif stage == 'report':
        from src.reporting import report
        report(paths['summary'], data_root=root, output_dir=paths['report'], primary_radius=options['primary_radius'], seed=options['seed'])
        seal(root / paths['report'])
    else:
        raise ValueError(f'unknown stage: {stage}')


def verify_run(run_dir, *, code_root, data_root, require_success=False):
    """Read-only integrity check. Does not modify run.json or rerun calculations."""
    run_dir = Path(run_dir)
    if run_dir.is_symlink() or not run_dir.resolve().is_relative_to(Path(data_root).resolve() / 'outputs'):
        raise ValueError('run directory escapes outputs or is a symlink')
    state = read_json(run_dir / 'run.json')
    if require_success and state['status'] != 'success':
        raise ValueError('run must be successful')
    if state['status'] not in ('partial', 'success'):
        raise ValueError(f'cannot verify {state["status"]} run as usable')
    if fingerprint(run_dir / 'snapshot.json') != state['snapshot_fingerprint']:
        raise ValueError('run snapshot changed')
    snap = read_json(run_dir / 'snapshot.json')
    if str(Path(code_root).resolve()) != snap['code_root'] or str(Path(data_root).resolve()) != snap['data_root']:
        raise ValueError('deployment roots changed; use a new run')
    if code_fingerprints(code_root, configuration_files(snap['config'])) != snap['code']:
        raise ValueError('code/configuration files changed')
    if dependency_fingerprint(code_root) != snap['dependencies']:
        raise ValueError('dependencies changed')
    if source_fingerprints(code_root, data_root, snap['study']) != snap['inputs']:
        raise ValueError('original inputs changed')
    completed = []
    count = 0
    for stage in STAGES:
        if stage not in state['stages']:
            continue
        item = state['stages'][stage]
        if item['status'] != 'completed':
            raise ValueError(f'stage {stage} is not complete')
        actual = artifact_hashes(run_dir / stage)
        if actual != item['artifacts']:
            raise ValueError(f'stage {stage}: artifacts changed')
        completed.append(stage)
        count += len(actual)
    if completed != list(STAGES[:len(completed)]) or not completed:
        raise ValueError('stage dependency history is incomplete')
    if (state['status'] == 'success') != (completed == list(STAGES)):
        raise ValueError('run status disagrees with completed stages')
    return {'status': 'passed', 'run_status': state['status'], 'run_id': state['run_id'],
            'completed_stages': completed, 'artifact_count': count, 'checked_at': timestamp(),
            'meaning': 'byte integrity and recorded stage completion; not a rerun of statistical correctness'}


def run_pipeline(config, study, *, run_id, stage='all', options=None, executor=execute_stage):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', run_id):
        raise ValueError('invalid run-id')
    if stage not in (*STAGES, 'all'):
        raise ValueError('invalid stage')
    root, code = Path(config['data_root']), Path(config['code_root'])
    run_dir = config['paths']['output_dir'] / run_id
    if run_dir.is_symlink() or not run_dir.resolve().is_relative_to(root.resolve() / 'outputs'):
        raise ValueError('run directory escapes outputs or is a symlink')
    defaults = dict(sample_stations=None, seed=20261012, primary_radius=1000, memory_limit='2GB', threads=1)
    options = {k: v for k, v in (options or {}).items() if v is not None}
    if options.keys() - defaults.keys():
        raise ValueError('unknown runtime option')
    effective = json.loads(json.dumps(config, default=str))
    # Preflight errors leave an existing valid run unchanged.
    if run_dir.exists():
        state = read_json(run_dir / 'run.json')
        snap = read_json(run_dir / 'snapshot.json')
        defaults.update(snap['options'])
        defaults.update(options)
        if stage == 'all' or stage in state['stages']:
            raise ValueError('stage already attempted; use a new run-id')
        verify_run(run_dir, code_root=code, data_root=root)
        if snap['config'] != effective or snap['study'] != study or snap['options'] != defaults:
            raise ValueError('configuration changed; use a new run-id')
    else:
        defaults.update(options)
        if stage not in ('all', 'inspect'):
            raise ValueError(f'{stage}: missing upstream stages')
        if defaults['sample_stations'] is not None and (type(defaults['sample_stations']) is not int or defaults['sample_stations'] <= 0):
            raise ValueError('sample_stations must be a positive integer')
        if type(defaults['seed']) is not int or defaults['primary_radius'] not in (500, 1000):
            raise ValueError('invalid seed or primary radius')
        if type(defaults['threads']) is not int or defaults['threads'] < 1 or not re.fullmatch(r'[1-9][0-9]*(MB|GB)', defaults['memory_limit']):
            raise ValueError('invalid threads or memory limit')
        snap = {'config': effective, 'study': study, 'options': defaults, 'code_root': str(code.resolve()),
                'data_root': str(root.resolve()), 'code': code_fingerprints(code, configuration_files(config)), 'dependencies': dependency_fingerprint(code),
                'inputs': source_fingerprints(code, root, study), 'created_at': timestamp()}
        run_dir.mkdir(parents=True, exist_ok=False)
        write_json(run_dir / 'snapshot.json', snap)
        state = {'schema_version': 1, 'run_id': run_id, 'status': 'partial', 'created_at': timestamp(),
                 'snapshot_fingerprint': fingerprint(run_dir / 'snapshot.json'), 'stages': {}}
        atomic_json(run_dir / 'run.json', state)
    requested = STAGES if stage == 'all' else (stage,)
    for name in requested:
        if any(state['stages'].get(prior, {}).get('status') != 'completed' for prior in STAGES[:STAGES.index(name)]):
            raise ValueError(f'{name}: missing upstream stage')
        if name in state['stages']:
            raise ValueError(f'{name}: already attempted')
        lock = run_dir / '.stage.lock'
        with lock.open('x') as stream:
            stream.write(str(os.getpid()))
        if read_json(run_dir / 'run.json') != state:
            lock.unlink()
            raise ValueError('run state changed concurrently; request rejected')
        begin = time.monotonic()
        item = {'status': 'running', 'started_at': timestamp()}
        state['stages'][name] = item
        atomic_json(run_dir / 'run.json', state)
        stage_dir = run_dir / name
        try:
            stage_dir.mkdir(exist_ok=False)
            def progress(event):
                with (stage_dir / 'events.jsonl').open('a') as stream:
                    stream.write(json.dumps({'time': timestamp(), 'event': event}, default=str) + '\n')
            print(json.dumps({'stage': name, 'status': 'started', 'run_id': run_id}), flush=True)
            executor(name, config=config, study=study, run_dir=run_dir, options=defaults, progress=progress)
            if code_fingerprints(code, configuration_files(config)) != snap['code'] or dependency_fingerprint(code) != snap['dependencies']:
                raise ValueError('code or dependencies changed while running')
            if source_fingerprints(code, root, study) != snap['inputs']:
                raise ValueError('original inputs changed while running')
            item.update(status='completed', finished_at=timestamp(), elapsed_seconds=time.monotonic() - begin,
                        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, artifacts=artifact_hashes(stage_dir))
            state['status'] = 'success' if all(state['stages'].get(s, {}).get('status') == 'completed' for s in STAGES) else 'partial'
            atomic_json(run_dir / 'run.json', state)
            verify_run(run_dir, code_root=code, data_root=root)
            print(json.dumps({'stage': name, 'status': 'completed', 'elapsed_seconds': item['elapsed_seconds']}), flush=True)
        except Exception as error:
            item.update(status='failed', error=f'{type(error).__name__}: {error}', elapsed_seconds=time.monotonic() - begin,
                        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, finished_at=timestamp())
            state['status'] = 'failed'
            atomic_json(run_dir / 'run.json', state)
            raise RuntimeError(f'{name}: {error}') from error
        finally:
            lock.unlink()
    return state
