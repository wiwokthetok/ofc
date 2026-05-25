#!/usr/bin/env python3
"""
OFC FanPass — Auto Claim + Rescue Sweep (v2: Pure HTTP, browser hanya untuk login)

Flow:
  1. Phase 1 — LOGIN (one-time):
       Playwright dipakai HANYA untuk login Google ke OneFootball. Setelah
       session.json terdeteksi valid (cek via API call /settings/profile),
       browser ditutup. Sesi tersimpan untuk run-run berikutnya.

  2. Phase 2 — CLAIM (no browser, pure HTTP+web3):
       Loop tiap PK di ok.txt:
         a. Pre-fetch: nonce, gas, allocation, eligibility — semua via requests
         b. Build claim() tx + sweep tx (pre-signed) di memory
         c. Tanya user y/n
         d. Broadcast claim tx → tunggu OFC masuk
         e. IMMEDIATELY broadcast sweep tx (OFC transfer + ETH sweep) dengan
            high-priority gas, race vs sweeper bot drainer

Files:
  ok.txt       — 1 PK per baris
  adres.txt    — wallet tujuan (clean)
  session.json — cookies + headers (auto, jangan edit)
  endpoints.json — API endpoints discovered (auto)
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------- Bootstrap deps ----------
def _ensure(pkg, mod=None):
    mod = mod or pkg
    try:
        __import__(mod)
    except ImportError:
        print(f"[bootstrap] installing {pkg}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pkg])

for pkg, mod in [("playwright", "playwright"), ("web3", "web3"), ("eth-account", "eth_account"),
                 ("requests", "requests"), ("rich", "rich"), ("aiohttp", "aiohttp")]:
    _ensure(pkg, mod)

import requests
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

# ---------- Constants ----------
HERE         = Path(__file__).resolve().parent
PROFILE_DIR  = HERE / "ofc_profile"
SESSION_FILE = HERE / "session.json"
ENDPOINTS_FILE = HERE / "endpoints.json"
PK_FILE      = HERE / "ok.txt"
DEST_FILE    = HERE / "adres.txt"
LOG_FILE     = HERE / "ofc.log"

CLAIM_URL    = "https://fanpass.onefootball.com/airdrop-claim"
OF_API       = "https://api.onefootball.com"
PROFILE_API  = f"{OF_API}/users-accounts-api/v1/settings/profile"

# Multi-RPC for parallel broadcast (race-mode: first confirms wins)
BASE_RPCS = [
    "https://base.drpc.org",                  # fastest in test (91ms)
    "https://base-rpc.publicnode.com",         # reliable (165ms)
    "https://mainnet.base.org",                # official (140ms)
    "https://base.meowrpc.com",
    "https://base-pokt.nodies.app",
    "https://base.gateway.tenderly.co",
    "https://developer-access-mainnet.base.org",
]
BASE_RPC     = BASE_RPCS[0]
BASE_CHAIN_ID = 8453

# Gas multipliers — race-mode (sweeper bot drainer biasanya pakai 1.5x)
GAS_MULT_CLAIM = 3.0
GAS_MULT_SWEEP = 4.0

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
]

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

console = Console()
w3 = Web3(Web3.HTTPProvider(BASE_RPC, request_kwargs={"timeout": 10}))

# ---------- Logging ----------
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    console.print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(re.sub(r"\[/?[a-z #]+\]", "", line) + "\n")
    except Exception:
        pass

# ---------- IDR rate ----------
_idr_cache = {"ts": 0, "rate": None}
def eth_to_idr_rate() -> float:
    if time.time() - _idr_cache["ts"] < 60 and _idr_cache["rate"]:
        return _idr_cache["rate"]
    for url, parser in [
        ("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=idr",
         lambda d: d["ethereum"]["idr"]),
        ("https://api.coinbase.com/v2/exchange-rates?currency=ETH",
         lambda d: float(d["data"]["rates"]["IDR"])),
    ]:
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": UA})
            rate = float(parser(r.json()))
            _idr_cache.update({"ts": time.time(), "rate": rate})
            return rate
        except Exception:
            continue
    return 60_000_000.0  # fallback

def fmt_idr(idr_amount: float) -> str:
    return f"Rp {idr_amount:,.0f}".replace(",", ".")

def fmt_eth_idr(eth_amount: float) -> str:
    rate = eth_to_idr_rate()
    idr = eth_amount * rate
    return f"{eth_amount:.8f} ETH  ≈  {fmt_idr(idr)}"

# ---------- File I/O ----------
def read_pks() -> list[tuple[str, str]]:
    if not PK_FILE.exists():
        console.print(f"[red]Missing {PK_FILE}.[/red] Buat file dengan 1 private key per baris (lihat ok.txt.example)")
        sys.exit(1)
    out = []
    for i, raw in enumerate(PK_FILE.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not line.startswith("0x"):
            line = "0x" + line
        if len(line) != 66:
            console.print(f"[red]ok.txt line {i}: invalid PK length: {len(line)}[/red]")
            continue
        try:
            acct = Account.from_key(line)
            out.append((acct.address, line))
        except Exception as e:
            console.print(f"[red]ok.txt line {i}: invalid PK: {e}[/red]")
    if not out:
        console.print("[red]Tidak ada PK valid di ok.txt[/red]")
        sys.exit(1)
    return out

def read_dest() -> str:
    if not DEST_FILE.exists():
        console.print(f"[red]Missing {DEST_FILE}.[/red] Buat file isi 1 address tujuan saja.")
        sys.exit(1)
    s = DEST_FILE.read_text().strip().split()[0]
    if not s.startswith("0x"):
        s = "0x" + s
    if not Web3.is_address(s):
        console.print(f"[red]adres.txt: '{s}' bukan address valid[/red]")
        sys.exit(1)
    return Web3.to_checksum_address(s)

def load_session() -> dict | None:
    if SESSION_FILE.exists():
        try: return json.loads(SESSION_FILE.read_text())
        except: return None
    return None

def save_session(data: dict):
    SESSION_FILE.write_text(json.dumps(data, indent=2))

def load_endpoints() -> dict:
    if ENDPOINTS_FILE.exists():
        try: return json.loads(ENDPOINTS_FILE.read_text())
        except: return {}
    return {}

def save_endpoints(data: dict):
    ENDPOINTS_FILE.write_text(json.dumps(data, indent=2))

# ---------- HTTP session (with cookies from Playwright) ----------
def build_requests_session(session_data: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Origin": "https://fanpass.onefootball.com",
        "Referer": "https://fanpass.onefootball.com/airdrop-claim",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    for c in session_data.get("cookies", []):
        s.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
    if session_data.get("auth_header"):
        s.headers["Authorization"] = session_data["auth_header"]
    return s

def is_session_valid(session_data: dict) -> bool:
    """Hit /settings/profile — 200 = logged in, lain = belum/expired."""
    if not session_data or not session_data.get("cookies"):
        return False
    try:
        s = build_requests_session(session_data)
        r = s.get(PROFILE_API, timeout=10)
        return r.status_code == 200
    except Exception:
        return False

# ---------- Playwright login (ONE-TIME) ----------
def ensure_chromium():
    chr_dir = Path.home() / ".cache" / "ms-playwright"
    if not any(chr_dir.glob("chromium-*")):
        console.print("[dim]Installing chromium browser...[/dim]")
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])

def setup_novnc_if_needed() -> bool:
    """Setup Xvfb + noVNC if no DISPLAY available. Returns True if running headed via noVNC."""
    if os.environ.get("DISPLAY"):
        return True
    if subprocess.run(["which", "Xvfb"], capture_output=True).returncode != 0:
        console.print("[yellow]Setup noVNC (untuk login di VPS tanpa GUI)...[/yellow]")
        subprocess.run(["apt-get", "update"], capture_output=True)
        subprocess.run(["apt-get", "install", "-y", "xvfb", "x11vnc", "websockify", "novnc"], capture_output=True)
    if subprocess.run(["which", "Xvfb"], capture_output=True).returncode != 0:
        log("[red]Xvfb gagal install. Login harus di laptop, lalu copy session.json ke VPS.[/red]")
        return False
    # start Xvfb
    subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1280x800x24"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    os.environ["DISPLAY"] = ":99"
    # start x11vnc
    vnc_pw = "ofc" + str(int(time.time()))[-5:]
    pw_file = Path.home() / ".ofcvncpw"
    subprocess.run(["x11vnc", "-storepasswd", vnc_pw, str(pw_file)], capture_output=True)
    subprocess.Popen(["x11vnc", "-display", ":99", "-rfbauth", str(pw_file),
                      "-listen", "0.0.0.0", "-rfbport", "5900", "-forever", "-shared"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    # start websockify + novnc
    novnc_path = "/usr/share/novnc"
    subprocess.Popen(["websockify", "--web", novnc_path, "6080", "localhost:5900"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    # detect public IP
    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text
    except Exception:
        ip = "<VPS_IP>"
    console.print(Panel.fit(
        f"[bold cyan]Buka URL ini di browser HP/laptop kamu:[/bold cyan]\n"
        f"  http://{ip}:6080/vnc.html?host={ip}&port=6080&autoconnect=true\n"
        f"  Password VNC: [bold]{vnc_pw}[/bold]\n\n"
        f"[yellow]Pastikan port 6080 open di firewall VPS (ufw allow 6080/tcp).[/yellow]",
        title="noVNC ACCESS", border_style="cyan"
    ))
    return True

async def playwright_login_flow() -> dict:
    """Open browser, wait for user login, return session cookies."""
    from playwright.async_api import async_playwright
    ensure_chromium()
    can_headed = setup_novnc_if_needed()
    PROFILE_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=not can_headed,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
            user_agent=UA,
        )
        page = await ctx.new_page()
        await page.goto(CLAIM_URL, wait_until="domcontentloaded", timeout=60000)

        console.print(Panel.fit(
            "[yellow]Login manual di browser:[/yellow]\n"
            "  1. Klik [bold]Sign in with OneFootball[/bold]\n"
            "  2. Login pakai Google (atau email/password)\n"
            "  3. Tunggu sampai kembali ke halaman claim\n\n"
            "[dim]Script auto-detect login via API verification (akurat 100%).[/dim]",
            title="LOGIN MANUAL", border_style="yellow"
        ))

        # Poll login state via API call (cookies in context)
        deadline = time.time() + 900  # 15 menit
        cookies = []
        with Progress(SpinnerColumn(), TextColumn("[cyan]Menunggu login...[/cyan]"),
                      transient=True, console=console) as prog:
            t = prog.add_task("login", total=None)
            while time.time() < deadline:
                cookies = await ctx.cookies()
                cookie_dict = {c["name"]: c["value"] for c in cookies if "onefootball" in c.get("domain", "")}
                if cookie_dict:
                    # Hit profile API with these cookies
                    try:
                        r = requests.get(PROFILE_API, cookies=cookie_dict, headers={"User-Agent": UA, "Origin": "https://fanpass.onefootball.com"}, timeout=8)
                        if r.status_code == 200:
                            log("[green]Login berhasil (verified via API).[/green]")
                            break
                    except Exception:
                        pass
                await asyncio.sleep(2)
            else:
                raise TimeoutError("Login timeout 15 menit. Coba lagi.")

        # Save cookies + try to extract any localStorage auth
        cookies = await ctx.cookies()
        local_storage = {}
        try:
            local_storage = await page.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
        except Exception:
            pass

        session_data = {
            "cookies": [{"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c.get("path", "/")}
                        for c in cookies],
            "local_storage": local_storage,
            "saved_at": datetime.now().isoformat(),
        }
        save_session(session_data)
        await ctx.close()
        return session_data

def ensure_logged_in() -> dict:
    """Returns valid session_data; runs login flow if needed."""
    session = load_session()
    if session and is_session_valid(session):
        log("[green]Sesi valid (verified via API).[/green]")
        return session
    if session:
        log("[yellow]Sesi expired. Re-login dibutuhkan.[/yellow]")
    else:
        log("[dim]Tidak ada sesi tersimpan. Login dibutuhkan.[/dim]")
    return asyncio.run(playwright_login_flow())

# ---------- OneFootball API client ----------
class OFClient:
    """Pure HTTP client. No browser."""
    def __init__(self, session_data: dict):
        self.session = build_requests_session(session_data)
        self.endpoints = load_endpoints()

    def profile(self) -> dict | None:
        try:
            r = self.session.get(PROFILE_API, timeout=10)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            log(f"profile() err: {e}")
        return None

    def fanpass_token(self) -> dict | None:
        """Returns claim allocation + contract address + proof, if eligible."""
        url = self.endpoints.get("token") or f"{OF_API}/fanpass-service/v1/token"
        try:
            r = self.session.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                self.endpoints["token"] = url
                save_endpoints(self.endpoints)
                return data
            log(f"  fanpass-token status={r.status_code}: {r.text[:200]}")
        except Exception as e:
            log(f"  fanpass-token err: {e}")
        return None

    def link_wallet_nonce(self, address: str) -> dict | None:
        """Get message to sign for linking wallet."""
        candidates = self.endpoints.get("link_nonce", []) or [
            f"{OF_API}/fanpass-service/v1/wallet/link-message",
            f"{OF_API}/fanpass-service/v1/wallet/nonce",
            f"{OF_API}/users-accounts-api/v1/wallets/nonce",
            f"{OF_API}/fanpass-service/v1/airdrop/link-message",
        ]
        if isinstance(candidates, str):
            candidates = [candidates]
        for url in candidates:
            try:
                # try GET with query, then POST with body
                r = self.session.get(url, params={"address": address}, timeout=8)
                if r.status_code == 200:
                    self.endpoints["link_nonce"] = url
                    save_endpoints(self.endpoints)
                    return r.json()
                r = self.session.post(url, json={"address": address}, timeout=8)
                if r.status_code == 200:
                    self.endpoints["link_nonce"] = url
                    save_endpoints(self.endpoints)
                    return r.json()
            except Exception:
                continue
        return None

    def link_wallet(self, address: str, signature: str, message: str) -> bool:
        candidates = self.endpoints.get("link_wallet", []) or [
            f"{OF_API}/fanpass-service/v1/wallet/link",
            f"{OF_API}/fanpass-service/v1/airdrop/link-wallet",
            f"{OF_API}/users-accounts-api/v1/wallets",
        ]
        if isinstance(candidates, str):
            candidates = [candidates]
        for url in candidates:
            try:
                r = self.session.post(url, json={"address": address, "signature": signature, "message": message}, timeout=8)
                if r.status_code < 300:
                    self.endpoints["link_wallet"] = url
                    save_endpoints(self.endpoints)
                    return True
            except Exception:
                continue
        return False

    def claim_auth(self, address: str) -> dict | None:
        """Get claim authorization (merkle proof / voucher / contract data)."""
        candidates = self.endpoints.get("claim_auth", []) or [
            f"{OF_API}/fanpass-service/v1/airdrop/claim",
            f"{OF_API}/fanpass-service/v1/claim",
            f"{OF_API}/fanpass-service/v1/airdrop/{address}",
            f"{OF_API}/fanpass-service/v1/eligibility",
        ]
        if isinstance(candidates, str):
            candidates = [candidates]
        for url in candidates:
            try:
                u = url.replace("{address}", address)
                r = self.session.get(u, params={"address": address}, timeout=8)
                if r.status_code == 200:
                    self.endpoints["claim_auth"] = url
                    save_endpoints(self.endpoints)
                    return r.json()
                r = self.session.post(u, json={"address": address}, timeout=8)
                if r.status_code == 200:
                    self.endpoints["claim_auth"] = url
                    save_endpoints(self.endpoints)
                    return r.json()
            except Exception:
                continue
        return None

# ---------- Web3 tx helpers ----------
def sign_message(pk: str, message: str) -> str:
    msg = encode_defunct(text=message)
    return Account.from_key(pk).sign_message(msg).signature.hex()

def get_current_gas() -> int:
    """Current network gas price (wei). Tries multiple RPCs."""
    for rpc in BASE_RPCS[:3]:
        try:
            r = requests.post(rpc, json={"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1}, timeout=3)
            return int(r.json()["result"], 16)
        except Exception:
            continue
    return int(0.05 * 1e9)  # 0.05 gwei fallback (Base typical)

def build_claim_tx(pk: str, claim_data: dict, nonce: int | None = None, gas_mult: float = GAS_MULT_CLAIM) -> dict:
    """
    Build the on-chain claim() call.
    claim_data keys:
      - contract_address
      - calldata (hex string starting with 0x)
      - chain_id (default Base 8453)
      - gas_limit (optional)
      - value (optional, default 0)
    """
    acct = Account.from_key(pk)
    chain_id = claim_data.get("chain_id", BASE_CHAIN_ID)
    contract = Web3.to_checksum_address(claim_data["contract_address"])
    calldata = claim_data["calldata"]
    base_gas = get_current_gas()
    gas_price = max(int(base_gas * gas_mult), int(0.1 * 1e9))  # min 0.1 gwei
    if nonce is None:
        nonce = w3.eth.get_transaction_count(acct.address)
    gas_limit = claim_data.get("gas_limit") or 300_000
    return {
        "to": contract,
        "data": calldata,
        "value": claim_data.get("value", 0),
        "nonce": nonce,
        "chainId": chain_id,
        "gas": gas_limit,
        "maxFeePerGas": gas_price,
        "maxPriorityFeePerGas": int(gas_price * 0.5),
    }

def sign_tx(pk: str, tx: dict) -> str:
    """Returns raw signed tx hex."""
    acct = Account.from_key(pk)
    signed = acct.sign_transaction(tx)
    return signed.raw_transaction.hex()

async def _post_rpc(rpc: str, raw: str, session) -> dict:
    """Send raw tx to one RPC. Returns dict with tx_hash or error."""
    body = {"jsonrpc": "2.0", "method": "eth_sendRawTransaction", "params": [raw if raw.startswith("0x") else "0x" + raw], "id": 1}
    try:
        async with session.post(rpc, json=body, timeout=6) as r:
            j = await r.json()
            if "result" in j:
                return {"rpc": rpc, "tx_hash": j["result"]}
            return {"rpc": rpc, "error": j.get("error", {}).get("message", "?")}
    except Exception as e:
        return {"rpc": rpc, "error": str(e)[:80]}

async def broadcast_parallel(raw: str) -> tuple[str | None, list]:
    """Broadcast raw tx to ALL Base RPCs simultaneously. Returns first tx_hash + all results."""
    import aiohttp
    async with aiohttp.ClientSession() as s:
        tasks = [asyncio.create_task(_post_rpc(rpc, raw, s)) for rpc in BASE_RPCS]
        results = []
        tx_hash = None
        for fut in asyncio.as_completed(tasks):
            res = await fut
            results.append(res)
            if res.get("tx_hash") and not tx_hash:
                tx_hash = res["tx_hash"]
                # don't break — let others finish for redundancy
        return tx_hash, results

def broadcast_parallel_sync(raw: str) -> tuple[str | None, list]:
    """Sync wrapper."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # already in async context
            raise RuntimeError("sync called from async")
    except RuntimeError:
        pass
    return asyncio.run(broadcast_parallel(raw))

def send_signed(pk: str, tx: dict) -> str:
    """Sign + broadcast via multi-RPC parallel. Returns tx_hash."""
    raw = sign_tx(pk, tx)
    h, results = broadcast_parallel_sync(raw)
    # log all RPC results
    success_count = sum(1 for r in results if r.get("tx_hash"))
    log(f"  [dim]Broadcast: {success_count}/{len(results)} RPCs accepted[/dim]")
    if not h:
        for r in results[:3]:
            log(f"  [dim]  {r['rpc']}: {r.get('error', 'no result')[:80]}[/dim]")
        raise RuntimeError("All RPCs rejected tx")
    return h

def pre_sign_sweep_txs(pk: str, dest: str, ofc_token_addr: str | None, claim_nonce: int) -> list[dict]:
    """Pre-build OFC transfer + ETH sweep txs with nonces claim_nonce+1 and +2."""
    acct = Account.from_key(pk)
    out = []
    gas_price = int(w3.eth.gas_price * 2.0)  # very high priority
    # OFC transfer (placeholder amount filled at runtime)
    if ofc_token_addr:
        out.append({
            "kind": "ofc_transfer",
            "token": Web3.to_checksum_address(ofc_token_addr),
            "nonce": claim_nonce + 1,
            "gas_price": gas_price,
        })
    out.append({
        "kind": "eth_sweep",
        "nonce": claim_nonce + 1 + (1 if ofc_token_addr else 0),
        "gas_price": gas_price,
    })
    return out

def execute_sweep(pk: str, dest: str, ofc_token_addr: str | None, expected_ofc_amount: int | None = None) -> dict:
    """Execute OFC transfer + ETH sweep with MULTI-RPC parallel broadcast."""
    out = {}
    acct = Account.from_key(pk)
    from_addr = acct.address
    base_gas = get_current_gas()
    gas_price = max(int(base_gas * GAS_MULT_SWEEP), int(0.2 * 1e9))

    nonce = w3.eth.get_transaction_count(from_addr)

    if ofc_token_addr:
        try:
            tok = w3.eth.contract(address=Web3.to_checksum_address(ofc_token_addr), abi=ERC20_ABI)
            # Try to use known amount first (faster — no RPC roundtrip)
            bal = expected_ofc_amount
            if bal is None or bal <= 0:
                bal = tok.functions.balanceOf(from_addr).call()
            if bal > 0:
                tx = tok.functions.transfer(dest, bal).build_transaction({
                    "from": from_addr,
                    "nonce": nonce,
                    "chainId": BASE_CHAIN_ID,
                    "gas": 80_000,
                    "maxFeePerGas": gas_price,
                    "maxPriorityFeePerGas": int(gas_price * 0.8),
                })
                raw = sign_tx(pk, tx)
                h, results = broadcast_parallel_sync(raw)
                ok = sum(1 for r in results if r.get("tx_hash"))
                out["ofc_transfer"] = h
                log(f"  [green]→ OFC transfer:[/green] {h} ({ok}/{len(results)} RPCs, amt={bal})")
                nonce += 1
            else:
                log("  [yellow]OFC balance 0, skip transfer[/yellow]")
        except Exception as e:
            log(f"  [yellow]OFC transfer skipped: {e}[/yellow]")

    try:
        gas_cost = 21000 * gas_price
        eth_bal = w3.eth.get_balance(from_addr)
        amt = eth_bal - gas_cost
        if amt > 0:
            tx = {
                "to": dest, "value": amt, "nonce": nonce, "chainId": BASE_CHAIN_ID,
                "gas": 21000, "maxFeePerGas": gas_price, "maxPriorityFeePerGas": int(gas_price * 0.8),
            }
            raw = sign_tx(pk, tx)
            h, results = broadcast_parallel_sync(raw)
            ok = sum(1 for r in results if r.get("tx_hash"))
            out["eth_sweep"] = h
            log(f"  [green]→ ETH sweep:[/green] {h} ({ok}/{len(results)} RPCs, {amt/1e18:.8f} ETH)")
        else:
            log(f"  [yellow]ETH balance ({eth_bal/1e18:.8f}) ≤ gas cost, skip sweep[/yellow]")
    except Exception as e:
        log(f"  [red]ETH sweep error: {e}[/red]")
    return out

# ---------- Auto-recon via Playwright (one-time per endpoint) ----------
def make_recon_wallet_js(address: str, chain_id: int = BASE_CHAIN_ID, rpc: str = BASE_RPC) -> str:
    """
    Injected wallet that captures eth_sendTransaction params instead of broadcasting.
    The TX data tells us exactly which contract + calldata is needed for claim.
    """
    return f"""
    (() => {{
      const ADDR = "{address.lower()}";
      const CHAIN_HEX = "0x{chain_id:x}";
      const RPC = "{rpc}";
      window.__capturedTxs = [];
      const listeners = new Map();

      async function rpcCall(method, params) {{
        const r = await fetch(RPC, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ jsonrpc: "2.0", id: 1, method, params }})
        }});
        const j = await r.json();
        return j.result;
      }}

      const provider = {{
        isMetaMask: true,
        isConnected: () => true,
        chainId: CHAIN_HEX,
        selectedAddress: ADDR,
        networkVersion: String({chain_id}),

        async request({{ method, params = [] }}) {{
          if (method === "eth_accounts" || method === "eth_requestAccounts") return [ADDR];
          if (method === "eth_chainId") return CHAIN_HEX;
          if (method === "net_version") return String({chain_id});
          if (method === "wallet_switchEthereumChain") return null;
          if (method === "wallet_addEthereumChain") return null;
          if (method === "wallet_getPermissions" || method === "wallet_requestPermissions") {{
            return [{{ parentCapability: "eth_accounts", caveats: [{{ type: "restrictReturnedAccounts", value: [ADDR] }}] }}];
          }}

          if (method === "eth_sendTransaction") {{
            // CAPTURE THE TX (don't broadcast)
            const tx = params[0];
            window.__capturedTxs.push({{ method, tx, captured_at: Date.now() }});
            console.log("[RECON] Captured eth_sendTransaction:", tx);
            // Return fake hash so dApp continues normally
            return "0x" + "0".repeat(64);
          }}
          if (method === "personal_sign" || method === "eth_sign" || method.startsWith("eth_signTypedData")) {{
            // Just return fake signature for recon
            window.__capturedTxs.push({{ method, params, captured_at: Date.now() }});
            return "0x" + "0".repeat(130);
          }}
          return await rpcCall(method, params);
        }},
        on(event, fn) {{ if (!listeners.has(event)) listeners.set(event, []); listeners.get(event).push(fn); }},
        removeListener(event, fn) {{ if (!listeners.has(event)) return; listeners.set(event, listeners.get(event).filter(x => x !== fn)); }},
      }};

      Object.defineProperty(window, "ethereum", {{ value: provider, writable: false, configurable: false }});
      window.dispatchEvent(new Event("ethereum#initialized"));
      const info = {{ uuid: "recon-ofc-uuid", name: "OFC Recon", icon: "data:image/svg+xml;base64,PHN2Zy8+", rdns: "ofc.recon" }};
      window.dispatchEvent(new CustomEvent("eip6963:announceProvider", {{ detail: {{ info, provider }} }}));
      window.addEventListener("eip6963:requestProvider", () => {{
        window.dispatchEvent(new CustomEvent("eip6963:announceProvider", {{ detail: {{ info, provider }} }}));
      }});
      console.log("[OFC recon] wallet installed");
    }})();
    """

async def playwright_recon_claim(address: str) -> dict | None:
    """
    Open browser, inject recon-wallet, wait for user to click Claim.
    Capture the eth_sendTransaction → that's the actual claim contract + calldata.
    Also intercept all API calls to api.onefootball.com for endpoint discovery.
    """
    from playwright.async_api import async_playwright
    ensure_chromium()
    can_headed = setup_novnc_if_needed()
    PROFILE_DIR.mkdir(exist_ok=True)

    api_calls = []
    inject_js = make_recon_wallet_js(address)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=not can_headed,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
            user_agent=UA,
        )
        page = await ctx.new_page()
        await page.add_init_script(inject_js)

        # Intercept API responses for endpoint discovery
        async def on_response(resp):
            try:
                url = resp.url
                if "api.onefootball.com" not in url:
                    return
                ct = resp.headers.get("content-type", "")
                if "json" not in ct:
                    return
                data = None
                try:
                    data = await resp.json()
                except Exception:
                    return
                api_calls.append({"url": url, "method": resp.request.method, "status": resp.status, "data": data})
            except Exception:
                pass
        page.on("response", on_response)

        await page.goto(CLAIM_URL, wait_until="domcontentloaded", timeout=60000)

        console.print(Panel.fit(
            "[yellow]>>> RECON MODE <<<[/yellow]\n"
            f"  1. Pastikan kamu sudah login (kalau belum, login dulu)\n"
            f"  2. Connect wallet → pilih [bold]Browser Wallet[/bold] / [bold]MetaMask[/bold]\n"
            f"     (sebenarnya itu wallet recon kita yang ke-inject)\n"
            f"  3. Klik tombol [bold]Claim[/bold]\n"
            f"  4. Script auto-capture tx data, lalu browser tutup.\n\n"
            f"[dim]Wallet recon: {address}[/dim]",
            title="AUTO-RECON CLAIM API", border_style="yellow"
        ))

        # Poll for captured tx (max 5 min)
        deadline = time.time() + 300
        captured = None
        with Progress(SpinnerColumn(), TextColumn("[cyan]Menunggu user klik Claim...[/cyan]"),
                      transient=True, console=console) as prog:
            prog.add_task("recon", total=None)
            while time.time() < deadline:
                txs = await page.evaluate("() => window.__capturedTxs || []")
                # Look for eth_sendTransaction
                for entry in txs:
                    if entry.get("method") == "eth_sendTransaction":
                        captured = entry["tx"]
                        break
                if captured:
                    break
                await asyncio.sleep(1)

        await ctx.close()

    if not captured:
        log("[red]Tidak ada tx ter-capture dalam 5 menit.[/red]")
        return None

    log(f"[green]Tx ter-capture![/green] to={captured.get('to')} data_len={len(captured.get('data') or '')}")

    # Build result
    result = {
        "contract_address": captured.get("to"),
        "calldata": captured.get("data"),
        "value": int(captured.get("value", "0x0"), 16) if isinstance(captured.get("value"), str) and captured.get("value", "").startswith("0x") else int(captured.get("value", 0) or 0),
        "chain_id": int(captured.get("chainId", "0x2105"), 16) if isinstance(captured.get("chainId"), str) and captured.get("chainId", "").startswith("0x") else BASE_CHAIN_ID,
        "gas_limit": int(captured.get("gas", "0x493e0"), 16) if isinstance(captured.get("gas"), str) and captured.get("gas", "").startswith("0x") else None,
        "endpoints": {},
    }

    # Mine API responses for token address + amount + endpoint names
    for call in api_calls:
        url = call["url"]
        data = call.get("data") or {}
        if not isinstance(data, dict):
            continue
        # Token address
        for k, v in data.items():
            if isinstance(v, str) and v.startswith("0x") and len(v) == 42:
                if any(kw in k.lower() for kw in ["token", "asset", "currency"]):
                    result.setdefault("token_address", v)
            if isinstance(v, (int, float, str)) and any(kw in k.lower() for kw in ["amount", "allocation", "balance"]):
                result.setdefault("amount", v)
        # Endpoint categorization
        if "claim" in url.lower():
            result["endpoints"]["claim_auth"] = url.split("?")[0]
        if "wallet" in url.lower() and "link" in url.lower():
            result["endpoints"]["link_wallet"] = url.split("?")[0]
        if "nonce" in url.lower() or "message" in url.lower():
            result["endpoints"]["link_nonce"] = url.split("?")[0]
        if "/token" in url.lower():
            result["endpoints"]["token"] = url.split("?")[0]

    return result

# ---------- Per-wallet flow (pure HTTP) ----------
def process_wallet(client: OFClient, addr: str, pk: str, dest: str):
    log(f"\n[bold cyan]═══ Wallet: {addr} ═══[/bold cyan]")

    # 1. Show ETH balance
    try:
        bal = w3.eth.get_balance(addr) / 1e18
        log(f"  ETH balance: {fmt_eth_idr(bal)}")
        if bal < 0.00005:
            log(f"  [red]⚠️  Saldo ETH terlalu kecil untuk bayar gas. Top-up dulu min 0.0001 ETH ke {addr}[/red]")
    except Exception as e:
        log(f"  ETH balance err: {e}")

    # 2. Fetch fanpass token (allocation)
    log("  [dim]Fetch allocation dari OneFootball API...[/dim]")
    token_resp = client.fanpass_token()
    if not token_resp:
        log("  [red]Tidak bisa fetch /fanpass-service/v1/token. Sesi mungkin expired atau API berubah.[/red]")
        return

    log(f"  [dim]Token response:[/dim] {json.dumps(token_resp, indent=2)[:500]}")

    # 3. Try claim_auth
    claim_data = client.claim_auth(addr)
    if claim_data:
        log(f"  [dim]Claim auth response:[/dim] {json.dumps(claim_data, indent=2)[:500]}")
    else:
        log("  [yellow]Claim auth endpoint belum ke-discover. Mungkin perlu link wallet dulu.[/yellow]")

    # 4. Link wallet if needed
    if not claim_data:
        log("  [dim]Coba link wallet ke OneFootball account...[/dim]")
        nonce_resp = client.link_wallet_nonce(addr)
        if nonce_resp:
            msg = nonce_resp.get("message") or nonce_resp.get("nonce") or json.dumps(nonce_resp)
            sig = sign_message(pk, msg)
            ok = client.link_wallet(addr, "0x" + sig if not sig.startswith("0x") else sig, msg)
            log(f"  Link wallet: {'OK' if ok else 'failed'}")
            if ok:
                claim_data = client.claim_auth(addr)

    if not claim_data or not claim_data.get("contract_address") or not claim_data.get("calldata"):
        log("  [red]Claim data tidak lengkap. Auto-recon via Playwright dibutuhkan.[/red]")
        log("  [yellow]Browser akan terbuka. Klik tombol Claim di halaman SEKALI.[/yellow]")
        log("  [yellow]Script akan capture API call & contract address otomatis.[/yellow]")
        ans = input("  Lanjut auto-recon? [y/N]: ").strip().lower()
        if ans != "y":
            log("  [yellow]Skipped.[/yellow]")
            return
        recon_result = asyncio.run(playwright_recon_claim(addr))
        if recon_result:
            # Save discovered endpoints
            current_endpoints = load_endpoints()
            current_endpoints.update(recon_result.get("endpoints", {}))
            if recon_result.get("contract_address"):
                current_endpoints["contract_address"] = recon_result["contract_address"]
            save_endpoints(current_endpoints)
            # Try again with discovered endpoints
            claim_data = {
                "contract_address": recon_result["contract_address"],
                "calldata": recon_result["calldata"],
                "chain_id": recon_result.get("chain_id", BASE_CHAIN_ID),
                "value": recon_result.get("value", 0),
                "gas_limit": recon_result.get("gas_limit"),
            }
            if recon_result.get("token_address"):
                claim_data["token_address"] = recon_result["token_address"]
            if recon_result.get("amount"):
                claim_data["amount"] = recon_result["amount"]
            log("  [green]Endpoint ter-discover. Lanjut claim via pure HTTP.[/green]")
        else:
            log("  [red]Auto-recon gagal. Tidak bisa lanjut.[/red]")
            return

    # 5. Build claim tx
    try:
        tx = build_claim_tx(pk, claim_data)
        gas_eth = tx["gas"] * tx["maxFeePerGas"] / 1e18
        log(f"  Estimasi gas claim: {fmt_eth_idr(gas_eth)}")
    except Exception as e:
        log(f"  [red]Build tx error: {e}[/red]")
        return

    # 6. Display + confirm
    amount_display = claim_data.get("amount") or token_resp.get("amount") or "?"
    log(f"\n  [bold]>>> Siap claim:[/bold] {amount_display} OFC ke {addr}")
    log(f"  [bold]>>> Lalu sweep ke:[/bold] {dest}")
    ans = input(f"\n  Lanjut? [y/N]: ").strip().lower()
    if ans != "y":
        log("  [yellow]Skipped by user.[/yellow]")
        return

    # 7. PRE-SIGN bundle (claim + sweep) BEFORE broadcast — saves ~10-50ms
    log("  [dim]Pre-sign claim + sweep bundle...[/dim]")
    ofc_addr = claim_data.get("token_address") or token_resp.get("token_address")
    expected_amount = claim_data.get("amount_wei") or claim_data.get("amount")
    if isinstance(expected_amount, str):
        try:
            # could be string of wei or decimal
            expected_amount = int(expected_amount)
        except Exception:
            expected_amount = None

    claim_raw = sign_tx(pk, tx)
    log(f"  [dim]Bundle ready. Claim raw size: {len(claim_raw)} bytes[/dim]")

    # 8. Broadcast claim — MULTI-RPC parallel
    t0 = time.time()
    log("  [magenta]>>> BROADCAST CLAIM (parallel multi-RPC) <<<[/magenta]")
    try:
        h, results = broadcast_parallel_sync(claim_raw)
        broadcast_time = (time.time() - t0) * 1000
        ok = sum(1 for r in results if r.get("tx_hash"))
        if not h:
            log("  [red]Semua RPC tolak tx claim:[/red]")
            for r in results[:3]:
                log(f"    {r['rpc']}: {r.get('error', '?')[:100]}")
            return
        log(f"  [green]Claim broadcast in {broadcast_time:.0f}ms[/green] ({ok}/{len(results)} RPCs accepted)")
        log(f"  [green]Claim tx:[/green] https://basescan.org/tx/{h}")
    except Exception as e:
        log(f"  [red]Claim broadcast error: {e}[/red]")
        return

    # 9. Wait for tx confirmation (Base ~2s blocks)
    log("  [dim]Tunggu claim confirmed...[/dim]")
    try:
        receipt = w3.eth.wait_for_transaction_receipt(h, timeout=60, poll_latency=0.5)
        if receipt.status != 1:
            log("  [red]Claim tx reverted![/red]")
            return
        log(f"  [green]Claim confirmed in block {receipt.blockNumber}.[/green]")
    except Exception as e:
        log(f"  [yellow]Receipt wait err: {e}[/yellow]")

    # 10. IMMEDIATELY sweep (multi-RPC parallel, 4x gas)
    log("  [bold magenta]>>> SWEEP TO CLEAN WALLET (parallel, 4x gas) <<<[/bold magenta]")
    sweep_result = execute_sweep(pk, dest, ofc_addr, expected_ofc_amount=expected_amount)
    log(f"  Sweep result: {sweep_result}")

# ---------- Main ----------
def main():
    console.print(Panel.fit(
        "[bold cyan]OFC FanPass — Auto Claim + Rescue Sweep[/bold cyan]\n"
        "[dim]v2: Pure HTTP claim (browser hanya untuk login Google)[/dim]",
        border_style="cyan"
    ))

    pks = read_pks()
    dest = read_dest()

    # Config table
    rate = eth_to_idr_rate()
    info = Table(show_header=True, header_style="bold magenta", title="Konfigurasi")
    info.add_column("Item", style="cyan"); info.add_column("Nilai", style="white")
    info.add_row("Wallet sumber", f"{len(pks)} wallet")
    info.add_row("Wallet tujuan (clean)", dest)
    info.add_row("Rate ETH/IDR", fmt_idr(rate))
    info.add_row("Chain", "Base (chainId 8453)")
    info.add_row("Mode", "Pure HTTP + web3 (cepat, no browser)")
    console.print(info)

    # Phase 1: ensure logged in
    session_data = ensure_logged_in()

    # Phase 2: pure HTTP claim
    client = OFClient(session_data)
    profile = client.profile()
    if profile:
        username = profile.get("username") or profile.get("display_name") or profile.get("email", "?")
        log(f"[green]Logged in sebagai:[/green] {username}")

    for i, (addr, pk) in enumerate(pks, 1):
        log(f"\n[bold]>>> [{i}/{len(pks)}] Processing {addr}[/bold]")
        process_wallet(client, addr, pk, dest)

    log("\n[bold green]Selesai. Sesi tersimpan untuk bulan depan.[/bold green]")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Dibatalkan user.[/yellow]")
