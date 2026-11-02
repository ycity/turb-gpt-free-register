# -*- coding: utf-8 -*-
"""手动把 VCC 卡信息填进一个 Stripe 结账长链，然后把窗口交给人点提交。

默认绝不提交。用本机已装的 Chrome 开有头窗口，填完停在提交按钮前。

    python tools/vcc_fill_checkout.py --url "https://checkout.stripe.com/..." \
        --email you@example.com [--proxy http://host:port] [--card-id xxx]

VCC 开放接口有来源 IP 白名单，本机 IP 不在白名单会直接 403。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="填写 Stripe 结账页（默认不提交）")
    ap.add_argument("--url", required=True, help="Stripe 结账长链")
    ap.add_argument("--email", required=True, help="结账页要填的邮箱")
    ap.add_argument("--proxy", default="", help="浏览器代理，建议与账号注册时一致")
    ap.add_argument("--card-id", default="", help="指定 VCC 卡 ID，留空自动挑一张")
    ap.add_argument("--cred-file", default="",
                    help="VCC 凭据文件路径，覆盖 VCC_CRED_FILE")
    ap.add_argument("--holder-id", type=int, default=None,
                    help="指定持卡人 ID；有多个 ACTIVE 持卡人时必须指定")
    ap.add_argument("--channel", default="chrome",
                    help="本机浏览器通道：chrome / msedge，默认 chrome")
    ap.add_argument("--timeout", type=float, default=1800.0,
                    help="交还窗口后最多等多少秒，默认 1800")
    ap.add_argument("--expect", type=float, default=None,
                    help="期望金额，覆盖 VCC_EXPECTED_AMOUNT")
    ap.add_argument("--expect-currency", default=None,
                    help="期望币种，覆盖 VCC_EXPECTED_CURRENCY；传空串表示不校验")
    ap.add_argument("--allow-submit", action="store_true",
                    help="危险：让程序自己点提交并真实扣款，默认关闭")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr)

    from config import vcc as cfg
    from core.vcc_checkout_pay import pay_checkout

    cfg.VCC_AUTO_PAY_ENABLED = True
    cfg.VCC_BROWSER_CHANNEL = args.channel
    cfg.VCC_KEEP_OPEN_TIMEOUT = args.timeout
    if args.cred_file:
        cfg.VCC_CRED_FILE = args.cred_file
    if args.holder_id is not None:
        cfg.VCC_HOLDER_ID = args.holder_id
    if args.card_id:
        cfg.VCC_CARD_ID = args.card_id
    if args.expect is not None:
        cfg.VCC_EXPECTED_AMOUNT = args.expect
    if args.expect_currency is not None:
        cfg.VCC_EXPECTED_CURRENCY = args.expect_currency

    if args.allow_submit:
        cfg.VCC_KEEP_OPEN = False
        cfg.VCC_AUTO_SUBMIT = True
        cfg.VCC_HEADLESS = False
        print("!! 已开启自动提交，闸门通过后会真实扣款", file=sys.stderr)
    else:
        cfg.VCC_KEEP_OPEN = True
        cfg.VCC_AUTO_SUBMIT = False

    result = pay_checkout(args.url, email=args.email, proxy=args.proxy, cfg=cfg)

    print("\n==== 结果 ====", file=sys.stderr)
    for key in ("stage", "ok", "submitted", "currency", "amount", "expected",
                "card_mask", "message"):
        print(f"{key}: {result.get(key)}", file=sys.stderr)
    filled = result.get("filled") or {}
    if filled:
        ok = [k for k, v in filled.items() if v]
        skipped = [k for k, v in filled.items() if not v]
        print(f"已填: {', '.join(ok)}", file=sys.stderr)
        if skipped:
            print(f"未填: {', '.join(skipped)}", file=sys.stderr)
    for shot in result.get("screenshots") or []:
        print(f"截图: {shot}", file=sys.stderr)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
