"""Build artifacts without installing anything; import the extracted wheel in a fresh process."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
host = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / ".hermes/hermes-agent").resolve()
scratch = Path(os.environ.get("TMPDIR", Path.home() / ".hermes/cache/scratch")).resolve()
scratch.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="supervisor-build-", dir=scratch) as temp:
    home = Path(temp) / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("plugins: {enabled: []}\n")
    env = {"HOME": temp, "HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": temp,
           "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "HERMES_DISABLE_LAZY_INSTALLS": "1"}
    env["ROUTING_SOURCE"] = str(Path(os.environ.get("ROUTING_SOURCE", root.parent / "hermes-delegate-routing")).resolve())
    site = host / "venv/lib/python3.11/site-packages"
    code = f"""import sys
sys.path.insert(0, {str(site)!r})
from setuptools.build_meta import build_wheel, build_sdist
print('wheel:', build_wheel({str(root / 'dist')!r}))
print('sdist:', build_sdist({str(root / 'dist')!r}))
"""
    subprocess.run([str(host / "venv/bin/python"), "-c", code], cwd=root, env=env, check=True)
    wheel = root / "dist/hermes_delegate_supervisor-0.3.0-py3-none-any.whl"
    artifact = Path(temp) / "artifact"
    probe = f"""import sys, zipfile, importlib.metadata as metadata, hashlib, tarfile
from pathlib import Path
with tarfile.open({str(root / 'dist/hermes_delegate_supervisor-0.3.0.tar.gz')!r}) as archive:
    for name in ('README.md', 'tests/host_fixtures.py', 'tests/test_regressions.py', 'tests/test_cli_delivery.py', 'tests/test_supervision_command.py', 'tests/test_default_policy.py', 'tests/test_busy_command.py', 'scripts/verify_offline.py', 'hermes_delegate_supervisor/adapters.py', 'hermes_delegate_supervisor/busy.py'):
        assert archive.extractfile('hermes_delegate_supervisor-0.3.0/' + name).read() == (Path({str(root)!r}) / name).read_bytes()
zipfile.ZipFile({str(wheel)!r}).extractall({str(artifact)!r})
for name in ('__init__.py', 'core.py', 'host.py', 'adapters.py', 'busy.py', 'plugin.yaml'):
    relative = Path('hermes_delegate_supervisor') / name
    assert hashlib.sha256((Path({str(artifact)!r}) / relative).read_bytes()).digest() == hashlib.sha256((Path({str(root)!r}) / relative).read_bytes()).digest()
sys.path.insert(0, {str(artifact)!r})
import hermes_delegate_supervisor as plugin
assert plugin.__file__.startswith({str(artifact)!r})
dist = metadata.distribution('hermes-delegate-supervisor')
ep = next(e for e in dist.entry_points if e.group == 'hermes_agent.plugins')
assert ep.name == 'delegate_supervisor' and callable(ep.load().register)
assert plugin.__version__ == '0.3.0'
print('artifact import:', plugin.__file__)
print('entry point:', ep)
print('artifact verified: wheel import and metadata; no installation')
"""
    subprocess.run([str(host / "venv/bin/python"), "-c", probe], cwd=temp, env=env, check=True)
    subprocess.run([str(host / "venv/bin/python"), str(root / "scripts/verify_offline.py"),
                    str(host), str(artifact)], cwd=temp, env=env, check=True)
