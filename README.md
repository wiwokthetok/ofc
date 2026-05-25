# OFC FanPass — Auto Claim + Rescue Sweep

Auto-claim airdrop OneFootball FanPass (OFC) lalu **langsung sweep** ke wallet bersih, supaya sweeper bot drainer tidak sempat ambil.

## Arsitektur v2 — Pure HTTP

- **Playwright HANYA untuk login Google** (one-time per session). Setelah session.json terbentuk, browser tidak dipakai lagi.
- **Claim murni via HTTP requests + web3.py** — cepat (<1 detik per request) tanpa overhead browser.
- **Auto-discovery API** — script otomatis cari endpoint OneFootball yang aktif.
- **Login detection akurat** — verifikasi via API call (`/users-accounts-api/v1/settings/profile`), bukan heuristic cookie.

## Setup

```bash
git clone https://github.com/wiwokthetok/ofc.git   # ganti dengan nama repo kamu
cd ofc
pip install playwright web3 eth-account requests rich
python -m playwright install chromium

# config
cp ok.txt.example ok.txt && nano ok.txt        # 1 private key per baris
cp adres.txt.example adres.txt && nano adres.txt  # 1 address wallet bersih

python p.py
```

## Flow

### Pertama kali run

1. Script cek session → belum ada → buka browser (Playwright)
2. Kamu login OneFootball pakai Google di browser
3. Script verify login via API → simpan `session.json` → tutup browser
4. Lanjut ke claim phase (no browser, pure HTTP)

### Run berikutnya (bulan depan dst)

1. Script load `session.json` → verify masih valid via API
2. Kalau valid → langsung ke claim phase (no browser)
3. Kalau expired → re-login (Playwright sekali lagi)

### Claim phase per wallet

1. GET `/fanpass-service/v1/token` → ambil allocation OFC + claim data
2. Sign message (jika perlu link wallet baru)
3. Build claim() tx via web3.py, gas 1.5× current network rate
4. Tampilkan: address, jumlah OFC, gas estimate (ETH + IDR real-time)
5. Tanya `y/n`
6. Broadcast claim tx via Base RPC
7. Tunggu OFC masuk (poll balance, max 2 menit)
8. IMMEDIATELY broadcast:
   - OFC transfer ke `adres.txt`
   - Sisa ETH sweep ke `adres.txt`
   - Gas 2× network rate untuk race-mode

## File

| File | Isi | Commit ke git? |
|---|---|---|
| `p.py` | Script utama | ya |
| `ok.txt` | Private keys (1 per baris) | **TIDAK** (di-gitignore) |
| `adres.txt` | Wallet tujuan (1 address) | **TIDAK** (di-gitignore) |
| `session.json` | Cookies OneFootball + auth | **TIDAK** (di-gitignore) |
| `endpoints.json` | API endpoints discovered (cache) | **TIDAK** (di-gitignore) |
| `ofc_profile/` | Playwright profile (browser state) | **TIDAK** (di-gitignore) |
| `ofc.log` | Log per aksi | **TIDAK** (di-gitignore) |

## ⚠️ Security

- **`ok.txt` = plaintext private key**. Siapapun yang baca file = punya wallet kamu.
  - **JALANKAN HANYA DI DEVICE BERSIH** (bukan device yang seed-nya pernah bocor)
  - Hapus `ok.txt` setelah selesai claim, atau pakai disk encryption
- **`adres.txt` = wallet BARU** di device bersih. Idealnya hardware wallet (Ledger/Trezor).
- `.gitignore` sudah protect `ok.txt`, `adres.txt`, `session.json` dari accidental commit.

## Di VPS (no GUI)

Login pertama kali butuh GUI. Pilihan:

**Opsi A — Login di laptop, copy session ke VPS:**
```bash
# laptop (GUI)
python p.py              # login Google manual, lalu Ctrl+C setelah login berhasil

# transfer ke VPS
tar -czf session.tgz session.json ofc_profile
scp session.tgz user@vps:~/ofc/

# VPS
tar -xzf session.tgz
python p.py              # langsung claim, no browser
```

**Opsi B — Login langsung di VPS via noVNC:**
Script auto-setup Xvfb + x11vnc + websockify + novnc kalau detect tidak ada DISPLAY. Akan print URL + password — buka di browser HP/laptop kamu untuk akses browser di VPS.

```bash
# allow port di firewall
sudo ufw allow 6080/tcp
python p.py
```

## Troubleshooting

**"Tidak bisa fetch /fanpass-service/v1/token"**
→ Sesi expired. Hapus `session.json` dan run lagi (akan re-login).

**"Claim data tidak lengkap"**
→ API OneFootball mungkin pakai endpoint baru yang belum saya cover. Saya tambah auto-discovery via Playwright traffic interception untuk learning otomatis.

**"⚠️ Saldo ETH terlalu kecil"**
→ Kirim min 0.0001 ETH ke wallet itu (via bridge / exchange Base) untuk bayar gas claim.

**Gas IDR berbeda dengan MetaMask**
→ Rate ETH/IDR dari CoinGecko, refresh tiap 60 detik. Network gas dari Base public RPC.

## Catatan teknis

- **Login detection:** HIT `https://api.onefootball.com/users-accounts-api/v1/settings/profile`. 200 = login, lain = belum. Tidak pakai heuristic cookie name.
- **Auto-discovery API:** mencoba beberapa URL kandidat untuk tiap endpoint (`/wallet/link`, `/airdrop/claim`, `/eligibility`, dll) sampai 200. URL yang work disimpan ke `endpoints.json`.
- **Race-mode sweep:** gas claim 1.5× network, gas sweep 2.0× network. Sweeper bot drainer biasanya pakai 1.5× — kalah priority.
- **Sweep ordering:** broadcast 2 tx (OFC transfer dengan nonce N, ETH sweep dengan nonce N+1). Keduanya masuk block yang sama atau berurutan.

## Roadmap

- [ ] Auto-recon via Playwright kalau API endpoint belum ke-discover (intercept request saat user manual klik Claim sekali)
- [ ] Pre-sign sweep txs sebelum claim broadcast, untuk bundle Flashbots
- [ ] Multi-RPC failover (kalau public RPC down)
- [ ] Telegram notification setelah claim sukses

## Lisensi

Personal use. Jangan share `ok.txt` / `adres.txt` ke siapapun.
