import re
from pathlib import Path

html = Path("webui/templates/index.html").read_text(encoding="utf-8")

table = html.split('<table class="accounts-table-v2">', 1)[1].split("</table>", 1)[0]
colgroup = table.split("<colgroup>", 1)[1].split("</colgroup>", 1)[0]
head = table.split("<thead>", 1)[1].split("</thead>", 1)[0]
print("colgroup cols:", len(re.findall(r"<col\b", colgroup)))
print("header ths   :", len(re.findall(r"<th\b", head)))

row = html.split("const rowHtmlV2 = (r) => `", 1)[1].split("</tr>`", 1)[0]
print("row tds      :", len(re.findall(r"<td\b", row)))
print("empty colspan:", re.search(r'colspan="(\d+)"[^>]*>暂无账号', html).group(1))

order_cols = re.findall(r'<col class="(col-[a-z]+)"', colgroup)
order_th = re.findall(r'<th class="(col-[a-z]+)"', head)
order_td = re.findall(r'<td class="(col-[a-z]+)"', row)
print("cols == ths  :", order_cols == order_th)
print("cols == tds  :", order_cols == order_td)
print("order        :", " ".join(order_cols))
print("proxy width  :", bool(re.search(r"\.accounts-table-v2 \.col-proxy \{", html)))
