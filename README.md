# GCash -> AVA API Load Test (Locust)

Skrip load test dengan alur **User Journey berurutan** (`SequentialTaskSet`) dan **delay otomatis setelah setiap endpoint** agar simulasi trafik menyerupai perilaku user asli.

---

## 1. Persiapan Awal (Sekali Saja)

Buka terminal di folder `H:\indivara\gcash\load-test`:

```powershell
# Buat virtual environment Python
python -m venv .venv

# Aktifkan virtual environment
.\.venv\Scripts\Activate.ps1

# Install Locust & library pendukung
pip install -r requirements.txt
```

Lalu copy file `.env.example` menjadi `.env`:

```powershell
Copy-Item .env.example .env
```

Edit `.env` sesuai kebutuhan:
- `BASE_URL` / `CUSTOMER_BASE_URL` / `UT_BASE_URL` / `TRANSACTION_BASE_URL`: alamat service/gateway.
- `ENABLE_AUTH`: `false` untuk service lokal tanpa gateway; `true` untuk API Gateway dengan B2B Token & HMAC-SHA512.
- `STEP_DELAY_MIN` & `STEP_DELAY_MAX`: rentang delay (detik) setelah setiap request endpoint (default `1.0`–`3.0` detik).
- `NEW_USER_WEIGHT` & `EXISTING_USER_WEIGHT`: perbandingan porsi user baru vs user existing (default `2:8`).

---

## 2. Alur User Journey

1. **`NewCustomerUser` (bobot default 20%)**:
   `validate-user` → `customer-personal/create` → `invaccount/create` → `customer-personal/byCifBank` → `ut/products` → `products/nav-histories` → `transaction/subscription` → `ut/balance-latest` → `order-histories/history`
2. **`ExistingCustomerUser` (bobot default 80%)**:
   `validate-user` → `customer-personal/byCifBank` *(ambil `lastVersionDate`)* → `invaccount/byCifBank` → `ut/balance-latest` → `order-histories/history` → `ut/products` → `products/nav-histories` → cabang aksi akhir (`60%` `subscription` / `25%` `redemption` / `15%` `PUT customer-personal/update`)

Setelah **setiap** langkah endpoint di atas, Locust otomatis menunggu `random(min-delay, max-delay)` detik sebelum lanjut ke endpoint berikutnya.

---

## 3. Cara Menjalankan (Via Web UI)

Pastikan `.venv` aktif, lalu jalankan:

```powershell
locust
```

Buka browser ke **`http://localhost:8089`**, lalu isi form:
1. **Number of users**: jumlah concurrent user virtual (misal `10` untuk pemanasan, `30`–`100` untuk load lebih tinggi).
2. **Ramp up**: misal `5` atau `10` user/detik.
3. **Min delay / Max delay**: atur jeda antar endpoint dalam detik (default `1.0`–`3.0`).
4. Klik **Start swarming**.

---

## 4. Cara Menjalankan Tanpa Browser (CLI / Headless)

Contoh menjalankan **20 user** selama **5 menit** dengan delay **1–3 detik** setelah setiap endpoint:

```powershell
locust --headless --users 20 --spawn-rate 5 --run-time 5m --min-delay 1.0 --max-delay 3.0
```

Simpan laporan HTML:

```powershell
locust --headless --users 50 --spawn-rate 5 --run-time 15m --min-delay 1.0 --max-delay 2.5 --html report_journey.html
```

---

## 5. Data Uji
- `data/customers.csv`: data customer untuk user existing & fallback.
- `data/redemption_customers.csv`: data customer khusus transaksi `redemption`.
