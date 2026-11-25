import csv
import os
import random
import threading
import time
import uuid

from locust import HttpUser, events, task

import config
from auth import auth_manager

# ==============================================================================
# 1. Custom Arguments di Locust Web UI & CLI (Multiplier & Scenario)
# ==============================================================================
@events.init_command_line_parser.add_listener
def on_init_command_line_parser(parser):
    parser.add_argument(
        "--multiplier",
        type=float,
        default=config.DEFAULT_MULTIPLIER,
        help="Pengali target p99.9 (contoh: 0.01 = 1%%, 0.1 = 10%%, 1.0 = 100%%, 1.5 = 150%%)",
    )
    parser.add_argument(
        "--scenario",
        type=str,
        choices=["peak", "coincident", "smoke"],
        default=config.DEFAULT_SCENARIO,
        help="Profil rate dasar: peak (66 TPS), coincident (76 TPS), smoke (12 TPS)",
    )


# ==============================================================================
# 2. Loader Data Uji (Customer CSV) & State untuk S9 (Duplicate partnerRefNo)
# ==============================================================================
def load_customers(filename: str = "customers.csv") -> list[dict[str, str]]:
    csv_path = os.path.join(os.path.dirname(__file__), "data", filename)
    with open(csv_path, mode="r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise RuntimeError(f"data/{filename} kosong!")
    return rows


CUSTOMERS = load_customers("customers.csv")
_redemption_csv = os.path.join(os.path.dirname(__file__), "data", "redemption_customers.csv")
REDEMPTION_CUSTOMERS = load_customers("redemption_customers.csv") if os.path.exists(_redemption_csv) else CUSTOMERS
_ref_lock = threading.Lock()
_last_sub_ref: str | None = None
_last_red_ref: str | None = None
_version_lock = threading.Lock()
_last_version_cache: dict[str, str] = {}


def next_partner_ref(prefix: str, is_sub: bool) -> str:
    global _last_sub_ref, _last_red_ref
    with _ref_lock:
        last_ref = _last_sub_ref if is_sub else _last_red_ref
        if last_ref and config.DUPLICATE_REF_RATE > 0 and random.random() < config.DUPLICATE_REF_RATE:
            return last_ref
        new_ref = f"GCASH-{prefix}-{uuid.uuid4().hex[:12].upper()}"
        if is_sub:
            _last_sub_ref = new_ref
        else:
            _last_red_ref = new_ref
        return new_ref


# ==============================================================================
# 3. Locust User dengan Open-Model Arrival Rate Pacing + Weighted Endpoints
# ==============================================================================
class AvaApiUser(HttpUser):
    host = config.BASE_URL

    def on_start(self):
        parsed = self.environment.parsed_options
        self.scenario = getattr(parsed, "scenario", config.DEFAULT_SCENARIO)
        self.multiplier = getattr(parsed, "multiplier", config.DEFAULT_MULTIPLIER)
        self.rates = config.get_target_rates(self.scenario, self.multiplier)
        self.total_tps = sum(self.rates.values())
        self.endpoints = list(self.rates.keys())
        self.weights = list(self.rates.values())
        self._last_task_duration = 0.0

    def wait_time(self):
        """Pacing Open-Model: menjaga total TPS sistem mendekati target_tps = base * multiplier."""
        runner = self.environment.runner
        user_count = max(1, runner.user_count if runner else 1)
        tps_per_user = self.total_tps / user_count
        target_interval = 1.0 / max(0.0001, tps_per_user)
        sleep_seconds = max(0.0, target_interval - self._last_task_duration)
        return sleep_seconds

    @task
    def dispatch_weighted_request(self):
        endpoint_key = random.choices(self.endpoints, weights=self.weights, k=1)[0]
        customer = random.choice(CUSTOMERS)

        start = time.perf_counter()
        try:
            getattr(self, f"_call_{endpoint_key}")(customer)
        finally:
            self._last_task_duration = time.perf_counter() - start

    # --------------------------------------------------------------------------
    # Helper HTTP Request dengan Header Auth & Nama Metric yang Rapi
    # --------------------------------------------------------------------------
    def _send(self, base_url: str, method: str, path: str, name: str, body: dict | list | None = None):
        headers = auth_manager.build_headers(method, path, body)
        url = f"{base_url}{path}"
        kwargs = {"headers": headers, "name": name, "catch_response": True}
        if body is not None:
            kwargs["json"] = body
        with self.client.request(method, url, **kwargs) as resp:
            if resp.status_code >= 400:
                snippet = resp.text.replace("\n", " ").strip()[:250]
                resp.failure(f"HTTP {resp.status_code}: {snippet}")
            else:
                resp.success()
            return resp

    # --------------------------------------------------------------------------
    # 12 Endpoint AVA sesuai Dokumen Load Test Scenario Design
    # --------------------------------------------------------------------------
    def _call_customer_by_cif(self, c: dict[str, str]):
        path = f"{config.CUSTOMER_PREFIX}/v1/customer-personal/byCifBank/{c['cifBank']}"
        resp = self._send(config.CUSTOMER_BASE_URL, "GET", path, "GET customer-personal/byCifBank")
        if resp is not None and resp.status_code == 200:
            try:
                version = (resp.json().get("data") or {}).get("lastVersionDate")
                if version:
                    with _version_lock:
                        _last_version_cache[c["cifBank"]] = version
            except Exception:
                pass

    def _call_balance_latest(self, c: dict[str, str]):
        path = f"{config.UT_PREFIX}/v1/ut/balance-latest/{c['cifBank']}"
        self._send(config.UT_BASE_URL, "GET", path, "GET ut/balance-latest")

    def _call_order_history(self, c: dict[str, str]):
        path = f"{config.TRANSACTION_PREFIX}/v1/order-histories/history/byCifBank/{c['cifBank']}"
        self._send(config.TRANSACTION_BASE_URL, "POST", path, "POST order-histories/history", body={})

    def _call_ut_products(self, c: dict[str, str]):
        path = f"{config.UT_PREFIX}/v1/ut/products"
        body = {
            "cifBank": c["cifBank"],
            "recommendation": False,
            "sort": "totalAum asc",
            "timePeriod": "",
            "size": 20,
            "offset": 0,
        }
        self._send(config.UT_BASE_URL, "POST", path, "POST ut/products", body=body)

    def _call_validate_user(self, c: dict[str, str]):
        path = f"{config.CUSTOMER_PREFIX}/v1/customer-personal/validate-user"
        body = {
            "cifBank": c["cifBank"],
            "mobileContactNo": c["mobileContactNo"],
            "birthDate": c["birthDate"],
        }
        self._send(config.CUSTOMER_BASE_URL, "POST", path, "POST customer-personal/validate-user", body=body)

    def _call_invaccount_by_cif(self, c: dict[str, str]):
        path = f"{config.CUSTOMER_PREFIX}/v1/invaccount/byCifBank/{c['cifBank']}"
        self._send(config.CUSTOMER_BASE_URL, "GET", path, "GET invaccount/byCifBank")

    def _call_nav_histories(self, c: dict[str, str]):
        path = f"{config.UT_PREFIX}/v1/ut/products/nav-histories/{c['productCode']}?type=all"
        self._send(config.UT_BASE_URL, "GET", path, "GET products/nav-histories")

    def _call_trx_subscription(self, c: dict[str, str]):
        path = f"{config.UT_PREFIX}/v1/transaction/subscription"
        body = {
            "orders": [
                {
                    "cifBank": c["cifBank"],
                    "invAccountNo": c["invAccountNo"],
                    "productCode": c["productCode"],
                    "stlAccountNo": c["stlAccountNo"],
                    "salesId": config.DEFAULT_SALES_ID,
                    "branchId": config.DEFAULT_BRANCH_ID,
                    "trxAmount": 1000.00,
                    "appliedCalculationType": 0,
                    "appliedFeeType": 0,
                    "feeByRate": 0,
                    "feeByAmount": 0,
                    "partnerRefNo": next_partner_ref("SUB", is_sub=True),
                }
            ]
        }
        self._send(config.UT_BASE_URL, "POST", path, "POST transaction/subscription", body=body)

    def _call_invaccount_create(self, c: dict[str, str]):
        path = f"{config.CUSTOMER_PREFIX}/v1/invaccount/create"
        unique_inv = f"I26{uuid.uuid4().hex[:8].upper()}"
        body = {
            "invAccountNo": unique_inv,
            "invAccountName": f"LoadTest {unique_inv}",
            "cifBank": c["cifBank"],
        }
        self._send(config.CUSTOMER_BASE_URL, "POST", path, "POST invaccount/create", body=body)

    def _call_customer_create(self, c: dict[str, str]):
        path = f"{config.CUSTOMER_PREFIX}/v1/customer-personal/create"
        unique_cif = f"LT{uuid.uuid4().hex[:10].upper()}"
        unique_id_num = str(random.randint(10**15, 10**16 - 1))
        body = {
            "cifBank": unique_cif,
            "salutationCode": "MR",
            "firstName": "Juan",
            "middleName": "Dela",
            "lastName": f"Cruz {unique_cif[-4:]}",
            "gender": 0,
            "citizenship": 0,
            "countryCode": "PH",
            "birthPlace": "Manila",
            "birthDate": "1990-05-15",
            "maritalStatus": "9f9c64cd-f08d-006e-e043-c0a844b7006e",
            "spouseName": "-",
            "spouseDateOfBirth": "1900-01-01",
            "educationBackground": "9f9c64cd-f092-006e-e043-c0a844b7006e",
            "religion": "9f9c64cd-f095-006e-e043-c0a844b7006e",
            "income": "9f9c64cd-f0ad-006e-e043-c0a844b7006e",
            "fundSource": "9f9c64cd-f139-006e-e043-c0a844b7006e",
            "investmentPurposes": ["f49f3b26-d3d0-449f-af09-13ec18be4927"],
            "identifications": [
                {
                    "idNumber": unique_id_num,
                    "idOwnerName": f"Juan Dela Cruz {unique_cif[-4:]}",
                    "idType": "e996a629-8054-4050-a9a5-34074248c8ed",
                    "idTypeCode": "PAS",
                    "issuedDate": "2020-01-20",
                    "expiredDate": "2030-12-31",
                }
            ],
            "businessProfile": {
                "organizationName": "GCash Inc",
                "officeAddress": "BGC Taguig City",
                "professionCode": "PM",
            },
            "contact": {
                "residentContactNo": "+639171234567",
                "mobileContactNo": "+639171234567",
                "emailAddress": f"{unique_cif.lower()}@gcash.com",
            },
        }
        self._send(config.CUSTOMER_BASE_URL, "POST", path, "POST customer-personal/create", body=body)

    def _call_trx_redemption(self, c: dict[str, str]):
        rc = random.choice(REDEMPTION_CUSTOMERS)
        path = f"{config.UT_PREFIX}/v1/transaction/redemption"
        body = {
            "orders": [
                {
                    "cifBank": rc["cifBank"],
                    "invAccountNo": rc["invAccountNo"],
                    "productCode": rc["productCode"],
                    "stlAccountNo": rc["stlAccountNo"],
                    "salesId": config.DEFAULT_SALES_ID,
                    "branchId": config.DEFAULT_BRANCH_ID,
                    "redemptionType": 1,
                    "redemptionByUnit": 100.0,
                    "appliedFeeType": 0,
                    "feeByRate": 0,
                    "feeByAmount": 0,
                    "partnerRefNo": next_partner_ref("RED", is_sub=False),
                }
            ]
        }
        self._send(config.UT_BASE_URL, "POST", path, "POST transaction/redemption", body=body)

    def _call_customer_update(self, c: dict[str, str]):
        cif = c["cifBank"]
        with _version_lock:
            version = _last_version_cache.pop(cif, None)
        if not version:
            # Ambil lastVersionDate dulu jika belum ada di cache (untuk service yg belum deploy N2)
            get_path = f"{config.CUSTOMER_PREFIX}/v1/customer-personal/byCifBank/{cif}"
            get_resp = self._send(config.CUSTOMER_BASE_URL, "GET", get_path, "GET customer-personal/byCifBank")
            if get_resp is not None and get_resp.status_code == 200:
                try:
                    version = (get_resp.json().get("data") or {}).get("lastVersionDate")
                except Exception:
                    version = None

        path = f"{config.CUSTOMER_PREFIX}/v1/customer-personal/update/{cif}"
        body = {
            "cifBank": cif,
            "firstName": "LoadTest",
            "lastName": "Updated",
            "gender": None,
            "citizenship": -1,
            "countryCode": "PH",
            "birthPlace": "Manila",
            "birthDate": c["birthDate"],
            "maritalStatus": "9f9c64cd-f08d-006e-e043-c0a844b7006e",
            "spouseName": "-",
            "spouseDateOfBirth": "1900-01-01",
            "educationBackground": "9f9c64cd-f092-006e-e043-c0a844b7006e",
            "religion": "9f9c64cd-f095-006e-e043-c0a844b7006e",
            "income": "9f9c64cd-f0ad-006e-e043-c0a844b7006e",
            "fundSource": "9f9c64cd-f139-006e-e043-c0a844b7006e",
            "investmentPurposes": ["f49f3b26-d3d0-449f-af09-13ec18be4927"],
            "identifications": [
                {
                    "idNumber": "1234567890123456",
                    "idOwnerName": "LoadTest Updated",
                    "idTypeCode": "PAS",
                    "issuedDate": "2020-01-20",
                    "expiredDate": "2031-05-28",
                }
            ],
            "businessProfile": {
                "organizationName": "GCash Inc",
                "officeAddress": "BGC Taguig City",
                "professionCode": "PM",
            },
            "contact": {
                "residentPhoneNo": "0211234567",
                "mobilePhoneNo": c["mobileContactNo"],
                "emailAddress": f"{cif.lower()}@example.com",
            },
            "settlementAccount": [
                {
                    "stlAccountNo": c["stlAccountNo"],
                    "holderName": "LoadTest Updated",
                    "accountTypeCode": "SAV",
                    "bankId": config.DEFAULT_BANK_ID,
                    "branchId": config.DEFAULT_BRANCH_ID,
                }
            ],
        }
        if version:
            body["lastVersionDate"] = version
        self._send(config.CUSTOMER_BASE_URL, "PUT", path, "PUT customer-personal/update", body=body)
