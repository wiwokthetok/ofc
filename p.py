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
                 ("requests", "requests"), ("rich", "rich")]:
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

BASE_RPC     = "https://base-rpc.publicnode.com"
BASE_CHAIN_ID = 8453

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
w3 = Web3(Web3.HTTPProvider(BASE_RPC))

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

def build_claim_tx(pk: str, claim_data: dict) -> dict:
    """
    Build the on-chain claim() call.
    claim_data keys (best-effort, real names from API will be auto-discovered):
      - contract_address
      - calldata (hex string starting with 0x — full tx data including selector + args)
      - chain_id (default Base 8453)
      - gas_limit (optional)
    """
    acct = Account.from_key(pk)
    chain_id = claim_data.get("chain_id", BASE_CHAIN_ID)
    contract = Web3.to_checksum_address(claim_data["contract_address"])
    calldata = claim_data["calldata"]
    gas_price = int(w3.eth.gas_price * 1.5)
    nonce = w3.eth.get_transaction_count(acct.address)
    gas_limit = claim_data.get("gas_limit") or 300_000
    tx = {
        "to": contract,
        "data": calldata,
        "value": 0,
        "nonce": nonce,
        "chainId": chain_id,
        "gas": gas_limit,
        "maxFeePerGas": gas_price,
        "maxPriorityFeePerGas": int(gas_price * 0.3),
    }
    return tx

def send_signed(pk: str, tx: dict) -> str:
    acct = Account.from_key(pk)
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    return h.hex()

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

def execute_sweep(pk: str, dest: str, ofc_token_addr: str | None) -> dict:
    """Execute OFC transfer + ETH sweep immediately."""
    out = {}
    acct = Account.from_key(pk)
    from_addr = acct.address

    if ofc_token_addr:
        try:
            tok = w3.eth.contract(address=Web3.to_checksum_address(ofc_token_addr), abi=ERC20_ABI)
            bal = tok.functions.balanceOf(from_addr).call()
            if bal > 0:
                gas_price = int(w3.eth.gas_price * 2.0)
                nonce = w3.eth.get_transaction_count(from_addr)
                tx = tok.functions.transfer(dest, bal).build_transaction({
                    "from": from_addr,
                    "nonce": nonce,
                    "chainId": BASE_CHAIN_ID,
                    "gas": 80_000,
                    "maxFeePerGas": gas_price,
                    "maxPriorityFeePerGas": int(gas_price * 0.5),
                })
                signed = acct.sign_transaction(tx)
                h = w3.eth.send_raw_transaction(signed.raw_transaction)
                out["ofc_transfer"] = h.hex()
                log(f"  [green]→ OFC transfer broadcast:[/green] {h.hex()} ({bal})")
            else:
                log("  [yellow]OFC balance 0, skip transfer[/yellow]")
        except Exception as e:
            log(f"  [yellow]OFC transfer skipped: {e}[/yellow]")

    try:
        gas_price = int(w3.eth.gas_price * 2.0)
        gas_cost = 21000 * gas_price
        eth_bal = w3.eth.get_balance(from_addr)
        amt = eth_bal - gas_cost
        if amt > 0:
            nonce = w3.eth.get_transaction_count(from_addr)
            tx = {
                "to": dest, "value": amt, "nonce": nonce, "chainId": BASE_CHAIN_ID,
                "gas": 21000, "maxFeePerGas": gas_price, "maxPriorityFeePerGas": int(gas_price * 0.5),
            }
            signed = acct.sign_transaction(tx)
            h = w3.eth.send_raw_transaction(signed.raw_transaction)
            out["eth_sweep"] = h.hex()
            log(f"  [green]→ ETH sweep broadcast:[/green] {h.hex()}  ({amt/1e18:.8f} ETH)")
        else:
            log(f"  [yellow]ETH balance ({eth_bal/1e18:.8f}) ≤ gas cost, skip sweep[/yellow]")
    except Exception as e:
        log(f"  [red]ETH sweep error: {e}[/red]")
    return out

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
        log("  [red]Claim data tidak lengkap. Endpoint mungkin perlu discover ulang via Playwright recon.[/red]")
        log(f"  [dim]Mau coba ulang dengan Playwright auto-discover? [y/N][/dim]")
        ans = input("  ").strip().lower()
        if ans == "y":
            log("  [yellow]TODO: Playwright auto-recon claim button click — coming in next iteration[/yellow]")
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

    # 7. Broadcast claim
    log("  [magenta]Broadcasting claim tx...[/magenta]")
    try:
        h = send_signed(pk, tx)
        log(f"  [green]Claim tx:[/green] https://basescan.org/tx/{h}")
    except Exception as e:
        log(f"  [red]Claim broadcast error: {e}[/red]")
        return

    # 8. Wait for OFC to arrive (or just confirmation)
    ofc_addr = claim_data.get("token_address") or token_resp.get("token_address")
    log("  [dim]Tunggu tx confirmed + OFC masuk...[/dim]")
    try:
        receipt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        if receipt.status != 1:
            log("  [red]Claim tx reverted![/red]")
            return
        log("  [green]Claim confirmed.[/green]")
    except Exception as e:
        log(f"  [yellow]Receipt wait err: {e}[/yellow]")

    # 9. IMMEDIATELY sweep
    log("  [bold magenta]>>> SWEEP TO CLEAN WALLET <<<[/bold magenta]")
    sweep_result = execute_sweep(pk, dest, ofc_addr)
    log(f"  Sweep tx: {sweep_result}")

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
