from locust import HttpUser, SequentialTaskSet, between, events, task

import csv
import os
import random
import threading
import uuid

import requests as raw_requests

import config
from auth import auth_manager

# ==============================================================================
# 1. Custom Arguments di Locust Web UI & CLI (Delay per Step)
# ==============================================================================
@events.init_command_line_parser.add_listener
def on_init_command_line_parser(parser):
    parser.add_argument(
        "--min-delay",
        type=float,
        default=config.STEP_DELAY_MIN,
        help="Delay minimum (detik) setelah setiap request endpoint",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=config.STEP_DELAY_MAX,
        help="Delay maksimum (detik) setelah setiap request endpoint",
    )


# ==============================================================================
# 2. Loader Data Uji & Shared State (S9 duplicate ref, version cache, CIF lock)
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
_cif_update_locks: dict[str, threading.Lock] = {}

_delay_bounds: tuple[float, float] = (config.STEP_DELAY_MIN, config.STEP_DELAY_MAX)


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Baca konfigurasi min-delay & max-delay dari Web UI atau CLI saat tes dimulai."""
    global _delay_bounds
    parsed = environment.parsed_options
    min_d = max(0.0, float(getattr(parsed, "min_delay", config.STEP_DELAY_MIN)))
    max_d = max(min_d, float(getattr(parsed, "max_delay", config.STEP_DELAY_MAX)))
    _delay_bounds = (min_d, max_d)


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


def _get_cif_lock(cif: str) -> threading.Lock:
    with _version_lock:
        if cif not in _cif_update_locks:
            _cif_update_locks[cif] = threading.Lock()
        return _cif_update_locks[cif]


def _extract_version_from_response(resp) -> str | None:
    if resp is None or resp.status_code != 200:
        return None
    try:
        return (resp.json().get("data") or {}).get("lastVersionDate")
    except Exception:
        return None


def _fetch_version_quietly(cif: str) -> str | None:
    """Fallback ambil lastVersionDate jika belum ada di cache sesi user."""
    path = f"{config.CUSTOMER_PREFIX}/v1/customer-personal/byCifBank/{cif}"
    url = f"{config.CUSTOMER_BASE_URL}{path}"
    headers = auth_manager.build_headers("GET", path, None)
    try:
        resp = raw_requests.get(url, headers=headers, timeout=15)
        return _extract_version_from_response(resp)
    except Exception:
        return None


# ==============================================================================
# 3. Helper Endpoint Bersama untuk User Journey
# ==============================================================================
class AvaEndpointMixin:
    """Kumpulan pemanggilan 12 endpoint AVA agar tidak duplikat antar journey."""

    def _send(self, base_url: str, method: str, path: str, name: str, body: dict | list | None = None):
        headers = auth_manager.build_headers(method, path, body)
        url = f"{base_url}{path}"
        kwargs = {"headers": headers, "name": name, "catch_response": True, "timeout": 30}
        if body is not None:
            kwargs["json"] = body
        with self.client.request(method, url, **kwargs) as resp:
            if resp.status_code >= 400:
                snippet = resp.text.replace("\n", " ").strip()[:250]
                resp.failure(f"HTTP {resp.status_code}: {snippet}")
            else:
                resp.success()
            return resp

    def call_validate_user(self, cif: str, mobile: str, birth_date: str):
        path = f"{config.CUSTOMER_PREFIX}/v1/customer-personal/validate-user"
        body = {
            "cifBank": cif,
            "mobileContactNo": mobile,
            "birthDate": birth_date,
        }
        return self._send(config.CUSTOMER_BASE_URL, "POST", path, "POST customer-personal/validate-user", body=body)

    def call_get_customer_by_cif(self, cif: str) -> str | None:
        path = f"{config.CUSTOMER_PREFIX}/v1/customer-personal/byCifBank/{cif}"
        resp = self._send(config.CUSTOMER_BASE_URL, "GET", path, "GET customer-personal/byCifBank")
        version = _extract_version_from_response(resp)
        if version:
            with _version_lock:
                _last_version_cache[cif] = version
        return version

    def call_get_invaccount_by_cif(self, cif: str):
        path = f"{config.CUSTOMER_PREFIX}/v1/invaccount/byCifBank/{cif}"
        return self._send(config.CUSTOMER_BASE_URL, "GET", path, "GET invaccount/byCifBank")

    def call_get_balance_latest(self, cif: str):
        path = f"{config.UT_PREFIX}/v1/ut/balance-latest/{cif}"
        return self._send(config.UT_BASE_URL, "GET", path, "GET ut/balance-latest")

    def call_post_order_history(self, cif: str):
        path = f"{config.TRANSACTION_PREFIX}/v1/order-histories/history/byCifBank/{cif}"
        return self._send(config.TRANSACTION_BASE_URL, "POST", path, "POST order-histories/history", body={})

    def call_post_ut_products(self, cif: str):
        path = f"{config.UT_PREFIX}/v1/ut/products"
        body = {
            "cifBank": cif,
            "recommendation": False,
            "sort": "totalAum asc",
            "timePeriod": "",
            "size": 20,
            "offset": 0,
        }
        return self._send(config.UT_BASE_URL, "POST", path, "POST ut/products", body=body)

    def call_get_nav_histories(self, product_code: str):
        path = f"{config.UT_PREFIX}/v1/ut/products/nav-histories/{product_code}?type=all"
        return self._send(config.UT_BASE_URL, "GET", path, "GET products/nav-histories")

    def call_create_customer(self):
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
        return self._send(config.CUSTOMER_BASE_URL, "POST", path, "POST customer-personal/create", body=body)

    def call_create_invaccount(self, cif: str) -> tuple[bool, str]:
        path = f"{config.CUSTOMER_PREFIX}/v1/invaccount/create"
        unique_inv = f"I26{uuid.uuid4().hex[:8].upper()}"
        body = {
            "invAccountNo": unique_inv,
            "invAccountName": f"LoadTest {unique_inv}",
            "cifBank": cif,
        }
        resp = self._send(config.CUSTOMER_BASE_URL, "POST", path, "POST invaccount/create", body=body)
        return (resp is not None and resp.status_code < 400), unique_inv

    def call_subscription(self, cif: str, inv_account_no: str, product_code: str, stl_account_no: str):
        path = f"{config.UT_PREFIX}/v1/transaction/subscription"
        body = {
            "orders": [
                {
                    "cifBank": cif,
                    "invAccountNo": inv_account_no,
                    "productCode": product_code,
                    "stlAccountNo": stl_account_no,
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
        return self._send(config.UT_BASE_URL, "POST", path, "POST transaction/subscription", body=body)

    def call_redemption(self, rc: dict[str, str]):
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
        return self._send(config.UT_BASE_URL, "POST", path, "POST transaction/redemption", body=body)

    def call_update_customer(self, c: dict[str, str], session_version: str | None = None):
        cif = c["cifBank"]
        with _get_cif_lock(cif):
            with _version_lock:
                cached = _last_version_cache.pop(cif, None)
            version = cached or session_version or _fetch_version_quietly(cif)

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
            return self._send(config.CUSTOMER_BASE_URL, "PUT", path, "PUT customer-personal/update", body=body)


# ==============================================================================
# 4. User Journey Berurutan (SequentialTaskSet + Delay Setelah Setiap Endpoint)
# ==============================================================================
class NewCustomerJourney(AvaEndpointMixin, SequentialTaskSet):
    """Alur user baru: validate -> create customer -> create invaccount -> detail -> browse -> subscribe -> balance/history."""

    def _reset_session(self):
        self.customer = random.choice(CUSTOMERS)
        self.probe_cif = f"NEW{uuid.uuid4().hex[:9].upper()}"
        self.probe_mobile = f"+63917{random.randint(1000000, 9999999)}"

    def on_start(self):
        self._reset_session()

    @task
    def step_1_validate_user(self):
        self._reset_session()
        self.call_validate_user(self.probe_cif, self.probe_mobile, "1990-05-15")

    @task
    def step_2_create_customer(self):
        self.call_create_customer()

    @task
    def step_3_create_invaccount(self):
        self.call_create_invaccount(self.customer["cifBank"])

    @task
    def step_4_get_customer_detail(self):
        self.call_get_customer_by_cif(self.customer["cifBank"])

    @task
    def step_5_browse_products(self):
        self.call_post_ut_products(self.customer["cifBank"])

    @task
    def step_6_view_nav_history(self):
        self.call_get_nav_histories(self.customer["productCode"])

    @task
    def step_7_subscribe(self):
        c = self.customer
        self.call_subscription(c["cifBank"], c["invAccountNo"], c["productCode"], c["stlAccountNo"])

    @task
    def step_8_check_balance(self):
        self.call_get_balance_latest(self.customer["cifBank"])

    @task
    def step_9_check_order_history(self):
        self.call_post_order_history(self.customer["cifBank"])


class ExistingCustomerJourney(AvaEndpointMixin, SequentialTaskSet):
    """Alur user existing: validate -> profil & akun -> portfolio/history -> browse produk/NAV -> sub/red/update."""

    def _pick_session_customer(self):
        self.customer = random.choice(CUSTOMERS)
        self.session_version: str | None = None

    def on_start(self):
        self._pick_session_customer()

    @task
    def step_1_validate_user(self):
        self._pick_session_customer()
        c = self.customer
        self.call_validate_user(c["cifBank"], c["mobileContactNo"], c["birthDate"])

    @task
    def step_2_get_customer_detail(self):
        self.session_version = self.call_get_customer_by_cif(self.customer["cifBank"])

    @task
    def step_3_get_invaccounts(self):
        self.call_get_invaccount_by_cif(self.customer["cifBank"])

    @task
    def step_4_check_balance(self):
        self.call_get_balance_latest(self.customer["cifBank"])

    @task
    def step_5_check_order_history(self):
        self.call_post_order_history(self.customer["cifBank"])

    @task
    def step_6_browse_products(self):
        self.call_post_ut_products(self.customer["cifBank"])

    @task
    def step_7_view_nav_history(self):
        self.call_get_nav_histories(self.customer["productCode"])

    @task
    def step_8_action(self):
        action = random.choices(
            ["subscription", "redemption", "update_profile"],
            weights=[60, 25, 15],
            k=1,
        )[0]
        c = self.customer
        if action == "subscription":
            self.call_subscription(c["cifBank"], c["invAccountNo"], c["productCode"], c["stlAccountNo"])
        elif action == "redemption":
            self.call_redemption(random.choice(REDEMPTION_CUSTOMERS))
        else:
            self.call_update_customer(c, session_version=self.session_version)


# ==============================================================================
# 5. Locust User Classes (Delay Otomatis Setelah Setiap Request Endpoint)
# ==============================================================================
class BaseAvaUser(HttpUser):
    abstract = True
    host = config.BASE_URL

    def wait_time(self):
        min_d, max_d = _delay_bounds
        return between(min_d, max_d)(self)


class NewCustomerUser(BaseAvaUser):
    weight = max(1, config.NEW_USER_WEIGHT)
    tasks = [NewCustomerJourney]


class ExistingCustomerUser(BaseAvaUser):
    weight = max(1, config.EXISTING_USER_WEIGHT)
    tasks = [ExistingCustomerJourney]
