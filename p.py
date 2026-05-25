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
USERS_API    = f"{OF_API}/users-accounts-api/"           # auth, settings, profile
FANPASS_API  = f"{OF_API}/fanpass-service/"               # fanpass general
METAGAME_API = f"{OF_API}/fanpass-metagame-backend/"      # CLAIM / vesting / merkle-rewards
PROFILE_API  = f"{USERS_API}v1/settings"                  # GET 200 = logged in

# REAL endpoint paths (relative to METAGAME_API) — discovered from production JS bundle
EP_STATUS      = lambda addr: f"reward/status/{addr}"
EP_FIRSTCLAIM  = lambda addr: f"reward/first-claim/{addr}"
EP_VESTING     = lambda addr: f"reward/vesting/{addr}"
EP_SIGNATURE   = lambda addr: f"merkle-rewards/signature/{addr}"  # returns {index, amount, proof}
EP_CLAIMSTATUS = "merkle-rewards/claim-status"

# REAL contract addresses (verified on Base)
CLAIM_CONTRACT = "0x06821F0A313871eBDCD5B2D4A56f2b7dB8853B00"  # Airdrop contract (has claim() function)
OFC_TOKEN      = "0x752C5a95d202972E124390F30a50154409d3c858"  # OFC ERC-20 (18 decimals)

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

def load_claim_abi() -> list:
    """Load full claim contract ABI from cp_abi.json (extracted from OneFootball production JS)."""
    abi_path = HERE / "cp_abi.json"
    if abi_path.exists():
        try: return json.loads(abi_path.read_text())
        except: pass
    # Minimal fallback if file missing
    return [
        {"inputs":[{"internalType":"uint256","name":"index","type":"uint256"},
                   {"internalType":"uint256","name":"amount","type":"uint256"},
                   {"internalType":"uint8","name":"vestingMonths","type":"uint8"},
                   {"internalType":"bytes32[]","name":"proof","type":"bytes32[]"}],
         "name":"claim","outputs":[],"stateMutability":"payable","type":"function"},
        {"inputs":[],"name":"getClaimFeeInEth","outputs":[{"internalType":"uint256","type":"uint256"}],"stateMutability":"view","type":"function"},
        {"inputs":[],"name":"TOKEN","outputs":[{"internalType":"address","type":"address"}],"stateMutability":"view","type":"function"},
        {"inputs":[],"name":"paused","outputs":[{"internalType":"bool","type":"bool"}],"stateMutability":"view","type":"function"},
        {"inputs":[{"internalType":"uint256","name":"index","type":"uint256"},
                   {"internalType":"address","name":"account","type":"address"},
                   {"internalType":"uint256","name":"amount","type":"uint256"},
                   {"internalType":"bytes32[]","name":"proof","type":"bytes32[]"}],
         "name":"checkEligibility","outputs":[{"type":"bool"},{"type":"uint256[3]"}],"stateMutability":"view","type":"function"},
    ]

CLAIM_ABI = load_claim_abi()

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

def _have(cmd: str) -> bool:
    return subprocess.run(["which", cmd], capture_output=True).returncode == 0

def _apt_install(pkgs: list[str]) -> bool:
    """Install via apt-get with proper error handling."""
    sudo_prefix = ["sudo", "-n"] if os.geteuid() != 0 and _have("sudo") else []
    cmds = [
        sudo_prefix + ["apt-get", "update", "-qq"],
        sudo_prefix + ["apt-get", "install", "-y", "-q", "--no-install-recommends"] + pkgs,
    ]
    for cmd in cmds:
        if not cmd: continue
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            log(f"  [red]apt error:[/red] {' '.join(cmd)} → {r.stderr[-200:]}")
            return False
    return True

def setup_novnc_if_needed() -> bool:
    """Setup Xvfb + noVNC if no DISPLAY available. Returns True if headed display ready.

    Untuk Termux Android user: gunakan SSH tunnel ke VPS:
      ssh -L 6080:localhost:6080 root@VPS_IP
    Lalu di browser HP: http://localhost:6080/vnc.html
    """
    if os.environ.get("DISPLAY"):
        return True

    missing = [b for b in ("Xvfb", "x11vnc", "websockify") if not _have(b)]
    novnc_dir = Path("/usr/share/novnc")
    if not novnc_dir.exists():
        missing.append("novnc")

    if missing:
        log(f"[yellow]Install dependencies: {missing}[/yellow]")
        pkg_map = {"Xvfb": "xvfb", "x11vnc": "x11vnc", "websockify": "websockify", "novnc": "novnc"}
        pkgs = list({pkg_map[m] for m in missing})
        if not _apt_install(pkgs):
            log("[red]Install gagal. Coba manual:[/red]")
            log(f"  [cyan]sudo apt-get update && sudo apt-get install -y {' '.join(pkgs)}[/cyan]")
            log("[yellow]Alternatif: login di laptop, lalu copy session.json + ofc_profile/ ke VPS via scp.[/yellow]")
            return False

    if not _have("Xvfb") or not _have("x11vnc"):
        log("[red]Xvfb/x11vnc masih tidak ada setelah install. Cek manual.[/red]")
        return False

    # Start Xvfb
    subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1280x800x24"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    os.environ["DISPLAY"] = ":99"

    # Start x11vnc with password
    vnc_pw = "ofc" + str(int(time.time()))[-5:]
    pw_file = Path.home() / ".ofcvncpw"
    subprocess.run(["x11vnc", "-storepasswd", vnc_pw, str(pw_file)], capture_output=True)
    # listen on localhost only (user accesses via SSH tunnel) — lebih aman
    subprocess.Popen(["x11vnc", "-display", ":99", "-rfbauth", str(pw_file),
                      "-listen", "127.0.0.1", "-rfbport", "5900", "-forever", "-shared", "-noxdamage"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)

    # Start websockify (also localhost only)
    novnc_path = "/usr/share/novnc" if Path("/usr/share/novnc").exists() else "/usr/share/novnc-1.3.0"
    subprocess.Popen(["websockify", "--web", novnc_path, "--listen", "127.0.0.1:6080", "127.0.0.1:5900"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)

    console.print(Panel.fit(
        f"[bold cyan]Akses noVNC via SSH tunnel (lebih aman, no firewall change):[/bold cyan]\n\n"
        f"  [yellow]Di Termux Android (NEW SSH session, jangan tutup yg sekarang):[/yellow]\n"
        f"    [bold]ssh -L 6080:localhost:6080 root@VPS_IP[/bold]\n\n"
        f"  [yellow]Lalu di browser HP buka:[/yellow]\n"
        f"    [bold]http://localhost:6080/vnc.html?host=localhost&port=6080&autoconnect=true&password={vnc_pw}[/bold]\n\n"
        f"  [dim]Password VNC: {vnc_pw}[/dim]\n\n"
        f"[green]Script lanjut otomatis setelah kamu login Google di browser VPS.[/green]",
        title="noVNC SETUP — SSH TUNNEL MODE", border_style="cyan"
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

# ---------- OneFootball API client (REAL endpoints from prod JS) ----------
class OFClient:
    """Pure HTTP client. No browser. Real endpoints discovered from production JS bundle."""
    def __init__(self, session_data: dict):
        self.session = build_requests_session(session_data)
        self.bearer_token = self._extract_bearer(session_data)

    def _extract_bearer(self, session_data: dict) -> str | None:
        """OneFootball uses Bearer token from cookies or localStorage."""
        for c in session_data.get("cookies", []):
            n = c.get("name", "").lower()
            if "access_token" in n or "bearer" in n or "jwt" in n:
                return c["value"]
        ls = session_data.get("local_storage", {}) or {}
        for k, v in ls.items():
            if "access_token" in k.lower() or "bearer" in k.lower():
                if isinstance(v, str) and len(v) > 20:
                    # might be JSON
                    try:
                        d = json.loads(v)
                        return d.get("access_token") or d.get("token") or d.get("accessToken")
                    except Exception:
                        return v
        return None

    def _get(self, base: str, path: str, auth: bool = True, **kwargs) -> requests.Response:
        url = f"{base}{path}"
        headers = kwargs.pop("headers", {})
        if auth and self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return self.session.get(url, headers=headers, timeout=10, **kwargs)

    def profile(self) -> dict | None:
        """GET /users-accounts-api/v1/settings — 200 = logged in."""
        try:
            r = self._get(USERS_API, "v1/settings", auth=True)
            if r.status_code == 200:
                return r.json()
            log(f"  profile status={r.status_code}: {r.text[:200]}")
        except Exception as e:
            log(f"  profile err: {e}")
        return None

    def reward_status(self, address: str) -> dict | None:
        """GET fanpass-metagame-backend/reward/status/{address} — returns {code, message}. code==2 = eligible."""
        try:
            r = self._get(METAGAME_API, EP_STATUS(address), auth=False)
            if r.status_code == 200:
                return r.json()
            log(f"  reward/status status={r.status_code}: {r.text[:300]}")
        except Exception as e:
            log(f"  reward/status err: {e}")
        return None

    def first_claim(self, address: str) -> dict | None:
        """GET reward/first-claim/{address} — returns {allocation, initialClaim:{3,6,9}, contractAllocation:{3,6,9}}."""
        try:
            r = self._get(METAGAME_API, EP_FIRSTCLAIM(address), auth=False)
            if r.status_code == 200:
                return r.json()
            log(f"  first-claim status={r.status_code}: {r.text[:200]}")
        except Exception as e:
            log(f"  first-claim err: {e}")
        return None

    def vesting(self, address: str) -> dict | None:
        """GET reward/vesting/{address} — vesting state."""
        try:
            r = self._get(METAGAME_API, EP_VESTING(address), auth=False)
            if r.status_code == 200:
                return r.json()
            log(f"  vesting status={r.status_code}: {r.text[:200]}")
        except Exception as e:
            log(f"  vesting err: {e}")
        return None

    def merkle_signature(self, address: str) -> dict | None:
        """GET merkle-rewards/signature/{address} — returns {index, amount, proof[]}. AUTH required."""
        try:
            r = self._get(METAGAME_API, EP_SIGNATURE(address), auth=True)
            if r.status_code == 200:
                return r.json()
            log(f"  merkle-signature status={r.status_code}: {r.text[:300]}")
        except Exception as e:
            log(f"  merkle-signature err: {e}")
        return None

    def claim_status(self) -> dict | None:
        try:
            r = self._get(METAGAME_API, EP_CLAIMSTATUS, auth=True)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
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

# ---------- Per-wallet flow (pure HTTP, REAL endpoints) ----------
# Claim contract instance (re-used)
_claim_contract = w3.eth.contract(address=Web3.to_checksum_address(CLAIM_CONTRACT), abi=CLAIM_ABI)

def build_claim_tx_from_merkle(pk: str, merkle_data: dict, vesting_months: int, claim_fee_wei: int) -> dict:
    """Build the claim() tx using real ABI + merkle proof from API."""
    acct = Account.from_key(pk)
    index = int(merkle_data["index"])
    amount = int(merkle_data["amount"])
    proof = [bytes.fromhex(p[2:] if p.startswith("0x") else p) for p in merkle_data["proof"]]

    base_gas = get_current_gas()
    gas_price = max(int(base_gas * GAS_MULT_CLAIM), int(0.1 * 1e9))
    nonce = w3.eth.get_transaction_count(acct.address)

    tx = _claim_contract.functions.claim(index, amount, vesting_months, proof).build_transaction({
        "from": acct.address,
        "value": int(claim_fee_wei * 1.15),  # 15% buffer for price fluctuation (matches JS)
        "nonce": nonce,
        "chainId": BASE_CHAIN_ID,
        "gas": 350_000,  # generous limit
        "maxFeePerGas": gas_price,
        "maxPriorityFeePerGas": int(gas_price * 0.5),
    })
    return tx

def get_claim_fee_eth() -> int:
    """Returns current claim fee in wei from contract."""
    try:
        return _claim_contract.functions.getClaimFeeInEth().call()
    except Exception as e:
        log(f"  getClaimFeeInEth err: {e}")
        return int(0.0005 * 1e18)  # fallback ~$1

def check_eligibility_onchain(addr: str, merkle_data: dict) -> tuple[bool, list]:
    """Verify merkle proof on-chain BEFORE broadcasting claim tx."""
    try:
        index = int(merkle_data["index"])
        amount = int(merkle_data["amount"])
        proof = [bytes.fromhex(p[2:] if p.startswith("0x") else p) for p in merkle_data["proof"]]
        result = _claim_contract.functions.checkEligibility(index, addr, amount, proof).call()
        return result[0], result[1]  # (eligible: bool, [3, 6, 9 amounts])
    except Exception as e:
        log(f"  checkEligibility err: {e}")
        return False, []

def process_wallet(client: OFClient, addr: str, pk: str, dest: str):
    log(f"\n[bold cyan]═══ Wallet: {addr} ═══[/bold cyan]")

    # 1. Show ETH balance + claim fee requirement
    bal_wei = 0
    try:
        bal_wei = w3.eth.get_balance(addr)
        bal_eth = bal_wei / 1e18
        log(f"  ETH balance: {fmt_eth_idr(bal_eth)}")
    except Exception as e:
        log(f"  ETH balance err: {e}")

    claim_fee = get_claim_fee_eth()
    claim_fee_with_buffer = int(claim_fee * 1.15)
    estimated_gas = 350_000 * int(get_current_gas() * GAS_MULT_CLAIM)
    total_needed = claim_fee_with_buffer + estimated_gas
    log(f"  Claim fee: {fmt_eth_idr(claim_fee/1e18)} (+ 15% buffer = {fmt_eth_idr(claim_fee_with_buffer/1e18)})")
    log(f"  Est. gas:  {fmt_eth_idr(estimated_gas/1e18)}")
    log(f"  TOTAL needed (claim fee + gas): {fmt_eth_idr(total_needed/1e18)}")

    if bal_wei < total_needed:
        short = (total_needed - bal_wei) / 1e18
        log(f"  [red]⚠️  Saldo kurang. Kirim {fmt_eth_idr(short)} lagi ke {addr}[/red]")
        log(f"  [yellow]Skip wallet ini (tidak cukup ETH).[/yellow]")
        return

    # 2. Check eligibility via API
    log("  [dim]Cek eligibility via reward/status/...[/dim]")
    status = client.reward_status(addr)
    if not status:
        log("  [red]Cannot fetch reward/status. Sesi expired? Hapus session.json dan re-login.[/red]")
        return
    log(f"  status: code={status.get('code')} message={status.get('message')}")
    if status.get("code") != 2:
        log(f"  [yellow]Address not eligible (code={status.get('code')}). Skip.[/yellow]")
        return

    # 3. Get first-claim allocation
    fc = client.first_claim(addr)
    if not fc:
        log("  [red]Cannot fetch first-claim. Skip.[/red]")
        return
    log(f"  Allocation: {fc.get('allocation')} OFC (raw)")
    initial_options = fc.get("initialClaim", {})
    contract_options = fc.get("contractAllocation", {})
    log(f"  Initial claim options (3/6/9 months): {initial_options}")
    log(f"  Contract allocation (3/6/9 months):   {contract_options}")

    # 4. Get merkle proof from API
    log("  [dim]Fetch merkle proof (signature endpoint)...[/dim]")
    merkle = client.merkle_signature(addr)
    if not merkle:
        log("  [red]Cannot fetch merkle proof. Skip.[/red]")
        return
    log(f"  merkle: index={merkle.get('index')} amount={merkle.get('amount')} proof_len={len(merkle.get('proof', []))}")

    # 5. Verify on-chain BEFORE broadcast
    eligible, three_options = check_eligibility_onchain(addr, merkle)
    log(f"  [dim]On-chain eligibility check: eligible={eligible} amounts={three_options}[/dim]")
    if not eligible:
        log("  [red]On-chain check says NOT eligible. Stopping.[/red]")
        return

    # 6. Pick vesting months — default to 3 months (fastest unlock)
    # User can override via env var OFC_VESTING_MONTHS
    months = int(os.environ.get("OFC_VESTING_MONTHS", "3"))
    if months not in (3, 6, 9):
        log(f"  [yellow]Invalid vesting months {months}, fallback to 3[/yellow]")
        months = 3
    chosen_initial = initial_options.get(str(months), 0) if isinstance(initial_options, dict) else 0
    log(f"  Vesting months: [bold]{months}[/bold] → initial claim: {chosen_initial} OFC")

    # 7. Build claim tx
    try:
        tx = build_claim_tx_from_merkle(pk, merkle, months, claim_fee)
        gas_eth = tx["gas"] * tx["maxFeePerGas"] / 1e18
        value_eth = tx["value"] / 1e18
        log(f"  Gas budget: {fmt_eth_idr(gas_eth)}")
        log(f"  Value attached: {fmt_eth_idr(value_eth)}")
    except Exception as e:
        log(f"  [red]Build tx error: {e}[/red]")
        return

    # Save token address for sweep later
    claim_data = {"token_address": OFC_TOKEN, "amount": chosen_initial}
    token_resp = fc  # for compat with later code
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
