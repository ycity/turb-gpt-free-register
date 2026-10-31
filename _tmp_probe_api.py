import json
from unittest.mock import patch

from core import db
from webui import app as webui_app

POOL = [
    "socks5h://user:pass@1.2.3.4:1080#美国家宽 1",
    "http://10.0.0.9:8080",
]
ROWS = [
    {"id": 3, "email": "named@x.test", "proxy_used": "socks5h://user:pass@1.2.3.4:1080"},
    {"id": 2, "email": "unnamed@x.test", "proxy_used": "http://10.0.0.9:8080"},
    {"id": 1, "email": "tag@x.test", "proxy_used": "skyvern:jp"},
]

app = webui_app.create_app(auth_code="probe-code-123")
app.config["TESTING"] = True
with patch.object(webui_app.config_editor, "get_field_value", return_value=list(POOL)), \
     patch.object(db, "_query_collection_page", side_effect=lambda *a, **kw: ([dict(r) for r in ROWS], 3, "")):
    client = app.test_client()
    resp = client.get("/api/accounts?paged=1&page=1&page_size=50",
                      headers={"X-Auth-Code": "probe-code-123"})
    print("status", resp.status_code)
    body = resp.get_json()
    for item in (body or {}).get("items", []):
        print(item["id"], item["email"], "->", json.dumps(item.get("register_proxy_name", None), ensure_ascii=False))
    print("leaks pass:", "pass@" not in resp.get_data(as_text=True))
