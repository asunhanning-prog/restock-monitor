#!/usr/bin/env python3
"""
奢侈品补货监控（Van Cleef & Arpels）

判断逻辑：
  页面 HTML 里 "ADD TO BAG" 和 "ORDER BY PHONE" 两个按钮一直同时存在，
  所以不能只搜文字，而是检查哪个按钮"真正可见"
  （display / visibility / opacity / hidden / 尺寸为 0 / 被推到屏幕外 / 被祖先元素裁掉）。
  有货按钮可见 => 有货。

退出码：
  0  正常完成（有货且已发出通知，或确认没货）
  1  两个按钮都看不到（页面异常、被拦截、改版等），已保存截图
  2  页面打不开 / 脚本运行出错
  3  有货，但所有通知渠道都发送失败

密码和 key 全部从环境变量读取（GitHub Secrets 会注入成环境变量）：
  BARK_KEY            Bark 的 Device Key（必填，用于推送）
  GMAIL_USER          发件 Gmail 地址（必填，用于邮件）
  GMAIL_APP_PASSWORD  Gmail 应用专用密码（必填，用于邮件）
  MAIL_TO             收件邮箱（选填，不填则发给 GMAIL_USER 自己）
  BARK_SERVER         选填，自建 Bark 服务器地址，默认 https://api.day.app
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import smtplib
import sys
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

# ======================= 可修改的配置 =======================
PRODUCT_URL = (
    "https://www.vancleefarpels.com/us/en/collections/jewelry/alhambra/"
    "vcard35600---vintage-alhambra-bracelet-5-motifs.html"
)
PRODUCT_NAME = "Van Cleef & Arpels Vintage Alhambra 手链 5 花 (VCARD35600)"

# 有货时显示的按钮文字、没货时显示的按钮文字（不区分大小写，可写多个备选）
IN_STOCK_TEXTS = ["ADD TO BAG"]
OUT_OF_STOCK_TEXTS = ["ORDER BY PHONE"]

TIMEZONE = "America/New_York"
SCREENSHOT_DIR = Path("screenshots")
# ===========================================================

EXIT_OK, EXIT_NEITHER, EXIT_ERROR, EXIT_NOTIFY_FAIL = 0, 1, 2, 3

# 在浏览器里执行：找出所有文字匹配的按钮，并判断每一个是否真正可见
DETECT_JS = r"""
({inTexts, outTexts}) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toUpperCase();

  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) return false;
    // 被推到页面左边/上边的屏幕外（如 left:-9999px）
    if (rect.right + window.scrollX <= 0 || rect.bottom + window.scrollY <= 0) return false;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none') return false;
      if (cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;
      if (parseFloat(cs.opacity) === 0) return false;
      if (n.hasAttribute('hidden')) return false;
      // 被带 overflow 的祖先元素完全裁掉（如 max-height:0; overflow:hidden）
      if (n !== el && cs.overflow !== 'visible') {
        const p = n.getBoundingClientRect();
        if (rect.right <= p.left || rect.left >= p.right ||
            rect.bottom <= p.top || rect.top >= p.bottom) return false;
      }
    }
    return true;
  };

  const sel = 'button, a, input[type="submit"], input[type="button"], [role="button"]';
  const all = Array.from(document.querySelectorAll(sel));

  const scan = (needles) => {
    const hits = [];
    for (const el of all) {
      const text = el.tagName === 'INPUT'
        ? norm(el.value)
        : norm(el.textContent) + ' ' + norm(el.getAttribute('aria-label'));
      for (const raw of needles) {
        const needle = norm(raw);
        // 文字要包含关键词，且不能是包着一大堆内容的容器
        if (text.includes(needle) && text.length <= needle.length + 40) {
          hits.push({ tag: el.tagName.toLowerCase(), text: text.trim().slice(0, 60), visible: isVisible(el) });
          break;
        }
      }
    }
    return hits;
  };

  const inHits = scan(inTexts);
  const outHits = scan(outTexts);
  return {
    in_found: inHits.length,
    out_found: outHits.length,
    in_visible: inHits.some(h => h.visible),
    out_visible: outHits.some(h => h.visible),
    in_hits: inHits,
    out_hits: outHits,
  };
}
"""


def log(msg: str) -> None:
    now = dt.datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def now_str() -> str:
    return dt.datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")


# --------------------------- 通知 ---------------------------
def send_bark(title: str, body: str, url: str) -> None:
    """Bark 紧急推送：level=critical + call=1，静音/专注/睡眠模式下也会响，并重复响铃。"""
    key = os.environ.get("BARK_KEY", "").strip()
    if not key:
        raise RuntimeError("没有设置 BARK_KEY（请在 GitHub Secrets 里添加）")
    key = key.rstrip("/").split("/")[-1]  # 容错：如果误贴了整条链接，只取最后一段
    server = os.environ.get("BARK_SERVER", "https://api.day.app").strip().rstrip("/")

    payload = {
        "device_key": key,
        "title": title,
        "body": body,
        "level": "critical",  # 紧急通知：突破静音、专注模式
        "call": "1",          # 重复响铃约 30 秒
        "volume": 10,         # 紧急通知音量 0-10
        "sound": "alarm",
        "group": "restock",
        "isArchive": "1",
        "url": url,           # 点通知直接打开商品页
    }
    r = requests.post(f"{server}/push", json=payload, timeout=20)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or data.get("code") not in (200, None):
        raise RuntimeError(f"Bark 返回异常：HTTP {r.status_code} {str(data)[:200]}")


def send_email(subject: str, body: str) -> None:
    """用 Gmail 应用专用密码（App Password）通过 SMTP 发邮件。"""
    user = os.environ.get("GMAIL_USER", "").strip()
    pwd = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    to = os.environ.get("MAIL_TO", "").strip() or user
    if not user or not pwd:
        raise RuntimeError("没有设置 GMAIL_USER 或 GMAIL_APP_PASSWORD（请在 GitHub Secrets 里添加）")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("补货监控", user))
    msg["To"] = to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, pwd)
        smtp.sendmail(user, [to], msg.as_string())


def notify_in_stock(url: str) -> bool:
    """有货时两个渠道都发；任意一个成功就返回 True。"""
    ok_any = False
    title = "🚨 有货了！Alhambra 手链"
    body = f"{PRODUCT_NAME}\n检测到「{IN_STOCK_TEXTS[0]}」按钮已出现\n{now_str()}"

    try:
        send_bark(title, body, url)
        log("Bark 紧急推送已发送")
        ok_any = True
    except Exception as e:  # noqa: BLE001
        log(f"Bark 发送失败：{type(e).__name__}: {e}")

    mail_body = (
        f"{PRODUCT_NAME} 现在显示「{IN_STOCK_TEXTS[0]}」，可能已补货。\n\n"
        f"检测时间：{now_str()}\n"
        f"商品链接：{url}\n\n"
        "热门单品通常很快售罄，建议立刻打开链接。"
    )
    try:
        send_email(f"🚨 有货了：{PRODUCT_NAME}", mail_body)
        log("邮件已发送")
        ok_any = True
    except Exception as e:  # noqa: BLE001
        log(f"邮件发送失败：{type(e).__name__}: {e}")

    return ok_any


# --------------------------- 页面检测 ---------------------------
def save_screenshot(page, tag: str) -> Path | None:
    try:
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        stamp = dt.datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y%m%d_%H%M%S")
        path = SCREENSHOT_DIR / f"{tag}_{stamp}.png"
        page.screenshot(path=str(path), full_page=True)
        try:
            (SCREENSHOT_DIR / f"{tag}_{stamp}.html").write_text(page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return path
    except Exception as e:  # noqa: BLE001
        log(f"保存截图失败：{type(e).__name__}: {e}")
        return None


def detect_state(page, wait_s: int, stable_needed: int = 3):
    """
    轮询页面，返回 ("IN" | "OUT" | "NONE", 最后一次检测详情)。
    同一状态连续出现 stable_needed 次（每次间隔 1 秒）才算数，避免页面加载中途的瞬间状态误报。
    """
    deadline = time.monotonic() + wait_s
    last, streak, snap = None, 0, {}
    while time.monotonic() < deadline:
        snap = page.evaluate(
            DETECT_JS, {"inTexts": IN_STOCK_TEXTS, "outTexts": OUT_OF_STOCK_TEXTS}
        )
        if snap["in_visible"]:
            state = "IN"  # 有货按钮可见 => 有货（优先于没货按钮）
        elif snap["out_visible"]:
            state = "OUT"
        else:
            state = None

        if state is None:
            last, streak = None, 0
        else:
            streak = streak + 1 if state == last else 1
            last = state
            if streak >= stable_needed:
                return state, snap
        page.wait_for_timeout(1000)

    return (last or "NONE"), snap


def check_stock(url: str, wait_s: int, headed: bool) -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            locale="en-US",
            timezone_id=TIMEZONE,
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            loaded = False
            for attempt in range(1, 4):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    loaded = True
                    break
                except PWError as e:
                    log(f"打开页面失败（第 {attempt}/3 次）：{str(e).splitlines()[0]}")
                    page.wait_for_timeout(3000)
            if not loaded:
                save_screenshot(page, "load_error")
                return EXIT_ERROR

            try:
                page.wait_for_load_state("load", timeout=15_000)
            except PWError:
                pass  # 有的网站一直有后台请求，不影响判断

            state, snap = detect_state(page, wait_s)
            log(
                f"检测结果={state} | 有货按钮：找到 {snap.get('in_found', 0)} 个，可见={snap.get('in_visible')} "
                f"| 没货按钮：找到 {snap.get('out_found', 0)} 个，可见={snap.get('out_visible')}"
            )

            if state == "IN":
                log("✅ 有货！")
                return EXIT_OK if notify_in_stock(url) else EXIT_NOTIFY_FAIL
            if state == "OUT":
                log("暂时没货。")
                return EXIT_OK

            # 两个按钮都看不到
            path = save_screenshot(page, "neither_visible")
            log(f"⚠️ 两个按钮都不可见（可能被拦截/改版/加载失败），截图：{path}")
            return EXIT_NEITHER
        finally:
            context.close()
            browser.close()


# --------------------------- 入口 ---------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="奢侈品补货监控")
    ap.add_argument("--test-email", action="store_true", help="只发一封测试邮件，然后退出")
    ap.add_argument("--test-bark", action="store_true", help="只发一条测试 Bark 紧急推送，然后退出")
    ap.add_argument("--url", default=PRODUCT_URL, help="商品链接（默认使用脚本里的链接）")
    ap.add_argument("--wait", type=int, default=25, help="最长等待页面按钮出现的秒数，默认 25")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（本机调试用）")
    args = ap.parse_args()

    if args.test_email or args.test_bark:
        failed = False
        if args.test_email:
            try:
                send_email(
                    "✅ 补货监控：测试邮件",
                    f"如果你收到这封邮件，说明 Gmail 配置正确。\n时间：{now_str()}",
                )
                log("测试邮件已发送，请检查收件箱（也看看垃圾邮件）")
            except Exception as e:  # noqa: BLE001
                log(f"测试邮件失败：{type(e).__name__}: {e}")
                failed = True
        if args.test_bark:
            try:
                send_bark(
                    "✅ 补货监控：测试推送",
                    f"如果手机响铃了，说明 Bark 紧急通知配置正确。\n{now_str()}",
                    args.url,
                )
                log("测试 Bark 推送已发送，请看手机")
            except Exception as e:  # noqa: BLE001
                log(f"测试 Bark 失败：{type(e).__name__}: {e}")
                failed = True
        return EXIT_NOTIFY_FAIL if failed else EXIT_OK

    try:
        return check_stock(args.url, args.wait, args.headed)
    except Exception as e:  # noqa: BLE001
        log(f"脚本出错：{type(e).__name__}: {e}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
