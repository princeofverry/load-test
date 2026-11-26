# GCash -> AVA API Load Test (Locust)

Skrip load test berbasis **User Journey realistis** dengan **proporsi hit mengikuti trafik produksi** ([config.py](file:///H:/indivara/gcash/load-test/config.py)) dan **delay otomatis setelah setiap endpoint**.

---

## 1. Persiapan Awal (Sekali Saja)

Buka terminal di folder `H:\indivara\gcash\load-test`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` sesuai kebutuhan:
- `BASE_URL` / `CUSTOMER_BASE_URL` / `UT_BASE_URL` / `TRANSACTION_BASE_URL`: alamat service/gateway.
- `ENABLE_AUTH`: `false` untuk service lokal tanpa gateway; `true` untuk API Gateway dengan B2B Token & HMAC-SHA512.
- `LOAD_SCENARIO`: `daily` (default), `peak`, `coincident`, atau `smoke`.
- `STEP_DELAY_MIN` & `STEP_DELAY_MAX`: rentang delay (detik) setelah setiap request endpoint (default `1.0`–`3.0` detik).

---

## 2. Alur & Proporsi Funnel User Journey (`AvaJourneyUser`)

1. **Mode `daily` (default — mengikuti rasio tabel `Req/day` produksi, total 1.469.731 req/day / Avg 17.01 TPS)**:
   - `GET customer-personal/byCifBank`: ~`37.4%` (`549,113/day`)
   - `GET ut/balance-latest`: ~`20.5%` (`301,330/day`)
   - `POST order-histories/history`: ~`18.4%` (`270,621/day`)
   - `POST ut/products`: ~`11.6%` (`169,769/day`)
   - `POST customer-personal/validate-user`: ~`4.6%` (`67,688/day`)
   - `GET invaccount/byCifBank`: ~`4.0%` (`59,081/day`)
   - `GET products/nav-histories`: ~`2.5%` (`37,417/day`)
   - `POST transaction/subscription`: ~`0.52%` (`7,637/day`)
   - `POST invaccount/create`: ~`0.19%` (`2,741/day`)
   - `POST customer-personal/create`: ~`0.13%` (`1,950/day`)
   - `POST transaction/redemption`: ~`0.10%` (`1,459/day`)
   - `PUT customer-personal/update`: ~`0.06%` (`925/day`)
2. **Mode `peak` (`p99.9` mix)**: menjaga alur journey dengan writes dinaikkan minimal `1.0` TPS (`22.8 : 12.5 : 11.2 : 7.0 : 2.8 : 2.5 : 2.0 : 1.6 : 1 : 1 : 1 : 1`).
3. **Mode `coincident`**: alur journey sesuai puncak bersamaan (`22 : 14 : 12 : 8 : 5 : 5 : 4 : 2 : 1 : 1 : 1 : 1`).
4. **Mode `smoke`**: membagi 50:50 dua alur berurutan sehingga ke-12 endpoint ter-hit dengan rasio **1:1**.

Delay `random(min-delay, max-delay)` hanya dijalankan **setelah request HTTP benar-benar ditembak**.

---

## 3. Cara Menjalankan

### Via Web UI
```powershell
locust
```
Buka **`http://localhost:8089`**, lalu isi:
- **Number of users**: jumlah concurrent user (misal `25`–`50` untuk lokal, `50`–`150` untuk UAT).
- **Ramp up**: misal `5` atau `10`.
- **Scenario**: `daily` *(default sesuai tabel Req/day)*, `peak`, `coincident`, atau `smoke`.
- **Min delay / Max delay**: jeda antar request dalam detik (default `1.0`–`3.0`).

### Via CLI / Headless
```powershell
locust --headless --users 30 --spawn-rate 5 --run-time 5m --scenario daily --min-delay 1.0 --max-delay 3.0
```
