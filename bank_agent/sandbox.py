"""Generated analysis code executes only in a resource-limited, networkless container."""

import ast
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from uuid import uuid4

from .errors import BankError

IMAGE = "bank-agent-analysis:local"
ALLOWED_IMPORTS = {"json", "math", "statistics", "collections"}


def validate_code(code):
    if len(code) > 12000:
        raise BankError("SANDBOX_CODE_SIZE", "统计代码过长")
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        raise BankError("SANDBOX_SYNTAX", "统计代码语法错误") from error
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else [node.module]
            if any(name not in ALLOWED_IMPORTS for name in names):
                raise BankError("SANDBOX_IMPORT", "统计代码包含非白名单导入")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise BankError("SANDBOX_PRIVATE_ACCESS", "禁止访问内部属性")
        if isinstance(node, ast.Name) and (
            node.id.startswith("__")
            or node.id
            in {
                "eval",
                "exec",
                "open",
                "compile",
                "input",
                "globals",
                "locals",
                "getattr",
                "setattr",
                "breakpoint",
                "print",
            }
        ):
            raise BankError("SANDBOX_UNSAFE_NAME", "统计代码包含禁止能力")
    return tree


def docker_args(directory):
    return [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=32",
        "--memory=128m",
        "--cpus=0.5",
        "--user=65534:65534",
        "--log-driver=none",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=8m",
        "--mount",
        f"type=bind,source={directory},target=/input,readonly",
        IMAGE,
        "python",
        "-I",
        "/input/runner.py",
    ]


def run_statistics(code, data):
    validate_code(code)
    if not shutil.which("docker"):
        raise BankError("SANDBOX_UNAVAILABLE", "未安装Docker；生成代码执行已关闭。内置账单分析仍可使用。", 503)
    try:
        check = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BankError("SANDBOX_UNAVAILABLE", "Docker未就绪，生成代码未执行", 503) from error
    if check.returncode:
        raise BankError("SANDBOX_IMAGE_MISSING", "先按部署说明构建统计沙箱镜像；不会自动拉取或在宿主机执行", 503)
    with tempfile.TemporaryDirectory(prefix="bank-statistics-") as directory:
        path = Path(directory).resolve()
        (path / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (path / "analysis.py").write_text(code, encoding="utf-8")
        (path / "runner.py").write_text(
            "import json\nfrom pathlib import Path\n"
            "data=json.loads(Path('/input/data.json').read_text())\n"
            "exec(compile(Path('/input/analysis.py').read_text(), 'analysis.py', 'exec'))\n"
            "output=json.dumps(result, ensure_ascii=False)\n"
            "assert len(output.encode()) <= 64000\nprint(output)\n",
            encoding="utf-8",
        )
        name = "bank-statistics-" + uuid4().hex[:16]
        args = docker_args(str(path))
        args[2:2] = ["--name", name]
        try:
            result = subprocess.run(args, capture_output=True, timeout=8)
        except subprocess.TimeoutExpired as error:
            raise BankError("SANDBOX_TIMEOUT", "统计代码执行超时，未生成分析结果", 408) from error
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=5)
        if result.returncode or len(result.stdout) > 64000:
            raise BankError("SANDBOX_EXECUTION_FAILED", "隔离统计代码执行失败或输出超限")
        try:
            return {
                "result": json.loads(result.stdout),
                "execution_mode": "isolated_statistics",
                "bank_mutations_allowed": False,
            }
        except ValueError as error:
            raise BankError("SANDBOX_RESULT_INVALID", "统计结果不是有效JSON") from error
