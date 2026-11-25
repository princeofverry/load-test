# GCash -> AVA API Load Test (Locust)

Panduan singkat untuk pemula menjalankan load test dengan **Multiplier** yang bisa diatur bebas (`0.01` = 1%, `0.10` = 10%, `1.0` = 100%, dst).

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
- `BASE_URL`: alamat API Gateway UAT (atau service lokal seperti `http://localhost:8083`).
- `ENABLE_AUTH`: set `false` jika nembak service lokal tanpa gateway; set `true` jika nembak API Gateway yang butuh B2B Token & HMAC-SHA512.

---

## 2. Cara Menjalankan (Via Web UI — Paling Gampang)

Pastikan `.venv` aktif, lalu jalankan:

```powershell
locust
```

Buka browser ke **`http://localhost:8089`**, lalu isi form-nya:
1. **Number of users**: isi sebagai *worker pool* (misal **`20`** untuk multiplier kecil, atau **`80`–`150`** untuk `1.0` / 100% ke atas). Jangan khawatir—skrip ini otomatis membatasi total TPS sesuai **`Multiplier`**, bukan sesuai jumlah user!
2. **Ramp up**: misal `10` (naik perlahan dalam 10 detik).
3. **Multiplier**: ketik bebas sesuai kebutuhan kamu:
   - `0.01` = **1%** dari p99.9 (~0,66 TPS)
   - `0.10` = **10%** dari p99.9 (~6,6 TPS)
   - `0.50` = **50%** dari p99.9 (~33 TPS)
   - `1.0` = **100%** dari p99.9 (~66 TPS — target utama S3)
   - `1.5` = **150%** (~99 TPS)
4. **Scenario**: pilih `peak` (default 66 TPS), `coincident` (76 TPS), atau `smoke` (12 TPS).
5. Klik **Start swarming**!

---

## 3. Cara Menjalankan Tanpa Browser (CLI / Headless)

Contoh menjalankan **10%** selama **5 menit**:

```powershell
locust --headless --users 30 --spawn-rate 5 --run-time 5m --multiplier 0.10 --scenario peak
```

Contoh menjalankan **100% (Peak S3)** selama **60 menit** sambil simpan laporan HTML:

```powershell
locust --headless --users 120 --spawn-rate 10 --run-time 60m --multiplier 1.0 --scenario peak --html report_s3.html
```

---

## 4. Menyiapkan Data Uji (`data/customers.csv`)
File `data/customers.csv` saat ini berisi 3 contoh data dari report Postman/Enhancement. Untuk pengujian UAT yang akurat (Section 6), isi file tersebut dengan **minimal 50.000 baris** customer UAT dengan format kolom yang sama:
```csv
cifBank,invAccountNo,stlAccountNo,productCode,mobileContactNo,birthDate
```
