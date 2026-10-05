"""Run in a separate, credential-free host interpreter. No installation or live turn."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
host = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / ".hermes/hermes-agent").resolve()
package_root = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else root
selection = sys.argv[3:] or [str(root / "tests")]
scratch = Path(os.environ.get("TMPDIR", Path.home() / ".hermes/cache/scratch")).resolve()
scratch.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="delegate-supervisor-", dir=scratch) as temporary:
    base = Path(temporary)
    home = base / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n")
    environment = {"HOME": str(base), "HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(base),
                   "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                   "ROUTING_SOURCE": str(Path(os.environ.get("ROUTING_SOURCE", root.parent / "hermes-delegate-routing")).resolve()), "HERMES_ENABLE_PROJECT_PLUGINS": "0", "HERMES_DISABLE_LAZY_INSTALLS": "1"}
    probe = f"""import sys, os
class NoLiveAgentImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {{'run_agent', 'hermes_bootstrap', 'hermes_cli.main', 'gateway.run', 'tui_gateway.server'}}:
            raise ImportError('Offline verification forbids bootstrap/live-agent import: ' + fullname)
sys.meta_path.insert(0, NoLiveAgentImports())
sys.path[:0] = [{str(package_root)!r}, {str(host)!r}, {str(host / 'venv/lib/python3.11/site-packages')!r}]
import hermes_cli.plugins, hermes_delegate_supervisor, pytest
print('host:', hermes_cli.plugins.__file__, flush=True)
print('plugin:', hermes_delegate_supervisor.__file__, flush=True)
assert hermes_cli.plugins.__file__.startswith({str(host)!r})
assert hermes_delegate_supervisor.__file__.startswith({str(package_root)!r})
raise SystemExit(pytest.main(['-q', '-s', '-p', 'no:cacheprovider', '--basetemp', {str(base / 'pytest')!r}, *{selection!r}]))
"""
    result = subprocess.run([str(host / "venv/bin/python"), "-c", probe], cwd=root, env=environment)
    raise SystemExit(result.returncode)
