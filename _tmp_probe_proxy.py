import sqlite3, collections

c = sqlite3.connect(r"E:\Github\turb-gpt-free-register\turb.sqlite3")
rows = c.execute("SELECT json_extract(payload, '$.proxy_used') FROM accounts").fetchall()
cnt = collections.Counter((v or "") for (v,) in rows)
print("total", len(rows))
for k, n in cnt.most_common(30):
    print(n, repr(k))
