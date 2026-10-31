"""提交到 web/ 的前端构建结果要完整，服务器不构建前端，直接用这份。"""

import re

from conftest import ROOT


def test_built_assets_exist_and_are_served(client):
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="root"' in html
    assets = re.findall(r'(?:src|href)="(/static/assets/[^"]+)"', html)
    assert any(a.endswith(".js") for a in assets)
    assert any(a.endswith(".css") for a in assets)
    for asset in assets:
        assert client.get(asset).status_code == 200, asset


def test_old_hand_written_files_are_gone():
    assert not (ROOT / "web" / "app.js").exists()
    assert not (ROOT / "web" / "style.css").exists()
