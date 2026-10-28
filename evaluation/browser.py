"""Validate visible-text replay artifacts before handing them to a judge."""
import hashlib
import json
from pathlib import Path
import zipfile


def ui_source_hash():
    paths = sorted(p for p in Path('bank_agent/static').iterdir() if p.suffix in {'.js', '.css', '.html'})
    rows = [(p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths]
    return hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()


def frozen_ui_source_hash(episode_path):
    root = Path(episode_path).parents[3]
    snapshot = root / 'source_snapshot.zip'
    if not snapshot.exists():
        return ui_source_hash()
    with zipfile.ZipFile(snapshot) as archive:
        names = sorted(n for n in archive.namelist() if n.startswith('bank_agent/static/') and Path(n).suffix in {'.js', '.css', '.html'})
        rows = [(Path(n).name, hashlib.sha256(archive.read(n)).hexdigest()) for n in names]
    if not rows:
        raise ValueError('Frozen UI sources are missing')
    return hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()


def load_browser_capture(episode_path, manifest_hash, clean_dom=False, revision='v2'):
    path = Path(episode_path)
    if revision not in {'v2', 'v3'}:
        raise ValueError('Unknown DOM capture revision')
    data = json.loads(path.with_name(f'browser_dom_{revision}.json' if clean_dom else 'browser_dom.json').read_text(encoding='utf-8'))
    if (data.get('version') != (f'browser-dom-regions-{revision}' if clean_dom else 'browser-dom-replay-v1') or data.get('manifest_hash') != manifest_hash
            or data.get('episode_hash') != hashlib.sha256(path.read_bytes()).hexdigest()
            or data.get('ui_source_hash') != frozen_ui_source_hash(path) or not data.get('api_calls_blocked')
            or data.get('real_interactive_execution') is not False):
        raise ValueError('Browser capture differs from the frozen episode or current UI')
    if clean_dom:
        script = path.parents[3]/f'capture_source_{revision}.cjs'
        if not script.exists() or hashlib.sha256(script.read_bytes()).hexdigest() != data.get('capture_script_hash'):
            raise ValueError('Region capture source changed')
        for frame in data['frames']:
            if frame.get('static_chrome_excluded') is not True or any(
                r.get('kind') not in {'assistant_reply','business_card','research_report','error_notice'} for r in frame.get('regions',[])
            ):
                raise ValueError('Invalid candidate region classification')
    return data
