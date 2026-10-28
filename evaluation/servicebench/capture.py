"""Serve static UI on an ephemeral localhost port; no application or bank database."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile
from threading import Thread


def capture(root, clean_dom=False, revision='v2'):
    root = Path(root).resolve()
    if not (root/"manifest.json").exists():
        raise ValueError("A frozen run manifest is required")
    project = Path(__file__).resolve().parents[2]

    class StaticHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/"):
                self.send_error(409, "Static evaluation replay cannot execute bank actions")
                return
            if self.path == "/":
                self.path = "/static/index.html"
            super().do_GET()

        def log_message(self, *args):
            pass

    staging = tempfile.TemporaryDirectory(prefix="bank-eval-ui-")
    serving = Path(staging.name)
    with zipfile.ZipFile(root/"source_snapshot.zip") as archive:
        for name in archive.namelist():
            if not name.startswith("bank_agent/static/"):
                continue
            relative = Path(name).relative_to("bank_agent")
            target = (serving/relative).resolve()
            if not target.is_relative_to(serving.resolve()):
                raise ValueError("Invalid UI snapshot path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(StaticHandler, directory=str(serving)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = Path.home()/".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
    node = shutil.which("node") or str(runtime/"bin/node.exe")
    environment = dict(os.environ)
    environment.setdefault("NODE_PATH", str(runtime/"node_modules"))
    try:
        if revision not in {'v2', 'v3'}:
            raise ValueError('Unknown DOM capture revision')
        script = f"capture_servicebench_dom_{revision}.cjs" if clean_dom else "capture_servicebench_dom.cjs"
        completed = subprocess.run([node, str(project/"scripts"/script), str(root),
            f"http://127.0.0.1:{server.server_port}", str(serving/"static")], cwd=project, env=environment, capture_output=True,
            encoding="utf-8", timeout=1800)
        if completed.returncode:
            raise RuntimeError("Static UI capture failed: "+completed.stderr[-1500:])
        return json.loads(completed.stdout.strip().splitlines()[-1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        staging.cleanup()
