"""Prepare a local environment and start Jianxing with one command."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv


def environment_file(root):
    """Create defaults without overwriting an existing configuration."""
    target = root / '.env'
    try:
        with target.open('xb') as stream:
            stream.write((root / '.env.example').read_bytes())
    except FileExistsError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description='Prepare and run Jianxing locally.')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--venv', type=Path, default=Path('.venv'))
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--setup-only', action='store_true', help='Install and configure without starting.')
    parser.add_argument('--update', action='store_true', help='Reinstall project dependencies.')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        parser.error('Python 3.12 or later is required.')
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535.')
    root = Path(__file__).resolve().parent
    env_dir = args.venv if args.venv.is_absolute() else root / args.venv
    python = env_dir / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.exists():
        print('Creating Python environment...', flush=True)
        venv.EnvBuilder(with_pip=True).create(env_dir)
    probe = subprocess.run([str(python), '-c', 'import bank_agent.app, uvicorn'], cwd=root,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if args.update or probe.returncode:
        print('Installing project dependencies...', flush=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-e', str(root)], cwd=root, check=True)
    environment_file(root)
    if args.setup_only:
        print('Environment ready. Run python run.py to start.', flush=True)
        return 0
    env = dict(os.environ)
    env.setdefault('BANK_SCHEDULER_MODE', 'embedded')
    env.setdefault('BANK_CHAT_WORKER_MODE', 'embedded')
    if args.data_dir:
        env['BANK_DATA_DIR'] = str(args.data_dir.resolve())
    print(f'Jianxing: http://{args.host}:{args.port}', flush=True)
    command = [str(python), '-m', 'uvicorn', 'bank_agent.main:app', '--host', args.host,
               '--port', str(args.port), '--workers', '1']
    with subprocess.Popen(command, cwd=root, env=env) as process:
        try:
            return process.wait()
        except KeyboardInterrupt:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
            return 0


if __name__ == '__main__':
    raise SystemExit(main())
