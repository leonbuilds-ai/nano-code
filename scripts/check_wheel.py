"""Verify the built wheel independently of the checkout's import path."""
import subprocess
import sys
import tempfile
from pathlib import Path
import zipfile

wheel = next(Path("dist").glob("*.whl"))
with tempfile.TemporaryDirectory() as directory:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "nanocode/static/index.html" in names, "Run the frontend build before packaging"
        assert any(name.endswith("repository-map.json") for name in names)
        assert any(name.endswith("/licenses/LICENSE") for name in names)
        archive.extractall(directory)
    code = r"""
import sys,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from nanocode.config import Settings
from nanocode.knowledge import KnowledgeStore
from nanocode.web import create_app
from fastapi.testclient import TestClient
with tempfile.TemporaryDirectory() as directory:
    root=Path(directory)
    settings=Settings(root,root/'data')
    app=create_app(settings)
    with TestClient(app,base_url='http://localhost:8865') as client:
        assert client.get('/api/status').json()['ok']
        page=client.get('/')
        assert page.status_code==200 and 'assets/' in page.text
        assert app.state.engine.store.route_skills('run tests')
print('Installed-wheel API, packaged UI and bundled skills: PASS')
"""
    subprocess.run([sys.executable, "-I", "-c", code, directory], check=True)
