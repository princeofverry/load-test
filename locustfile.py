from locust import HttpUser, constant, events, task

import csv
import os
import random
import threading
import time
import uuid

import requests as raw_requests

import config
from auth import auth_manager

# ==============================================================================
# 1. Custom Arguments di Locust Web UI & CLI (Scenario & Delay per Step)
# ==============================================================================
@events.init_command_line_parser.add_listener
def on_init_command_line_parser(parser):
    parser.add_argument(
        "--scenario",
        type=str,
        choices=["daily", "peak", "coincident", "smoke"],
        default=config.DEFAULT_SCENARIO,
        help="Profil proporsi trafik: daily (rasio Req/day produksi), peak (p99.9 mix), coincident, atau smoke (1:1)",
    )
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
# 2. Loader Data Uji, Shared State, dan Profil Proporsi Journey
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

# Probabilitas funnel per sesi agar urutan tetap realistis & proporsi hit mengikuti target produksi
SCENARIO_PROFILES: dict[str, dict[str, float]] = {
    # Target Req/day (1,469,731 total): byCifBank 549,113 | balance 301,330 | order_history 270,621 |
    # ut_products 169,769 | validate 67,688 | invaccount/byCifBank 59,081 | nav 37,417 |
    # sub 7,637 | inv_create 2,741 | create 1,950 | red 1,459 | update 925
    "daily": {
        "onboarding_prob": 1950.0 / 549113.0,
        "validate_prob": (67688.0 - 1950.0) / 547163.0,
        "invaccount_by_cif_prob": 59081.0 / 547163.0,
        "extra_invaccount_prob": (2741.0 - 1950.0) / 547163.0,
        "balance_latest_prob": 301330.0 / 547163.0,
        "order_history_prob": 270621.0 / 547163.0,
        "ut_products_prob": (169769.0 - 1950.0) / 547163.0,
        "nav_given_products_prob": 37417.0 / (169769.0 - 1950.0),
        "sub_prob": (7637.0 - 1950.0) / 547163.0,
        "red_prob": 1459.0 / 547163.0,
        "update_prob": 925.0 / 547163.0,
    },
    # Target ratio: byCifBank 22.8, balance 12.5, order_history 11.2, ut_products 7.0,
    # validate 2.8, invaccount/byCifBank 2.5, sub 2.0, nav 1.6, create/inv_create/red/update 1.0
    "peak": {
        "onboarding_prob": 1.0 / 22.8,
        "validate_prob": 1.8 / 21.8,
        "invaccount_by_cif_prob": 2.5 / 21.8,
        "extra_invaccount_prob": 0.0,
        "balance_latest_prob": 12.5 / 21.8,
        "order_history_prob": 11.2 / 21.8,
        "ut_products_prob": 6.0 / 21.8,
        "nav_given_products_prob": 1.6 / 6.0,
        "sub_prob": 1.0 / 21.8,
        "red_prob": 1.0 / 21.8,
        "update_prob": 1.0 / 21.8,
    },
    # Target ratio: byCifBank 22.0, balance 14.0, order_history 12.0, ut_products 8.0,
    # validate 5.0, invaccount/byCifBank 5.0, nav 4.0, sub 2.0, create/inv_create/red/update 1.0
    "coincident": {
        "onboarding_prob": 1.0 / 22.0,
        "validate_prob": 4.0 / 21.0,
        "invaccount_by_cif_prob": 5.0 / 21.0,
        "extra_invaccount_prob": 0.0,
        "balance_latest_prob": 14.0 / 21.0,
        "order_history_prob": 12.0 / 21.0,
        "ut_products_prob": 7.0 / 21.0,
        "nav_given_products_prob": 4.0 / 7.0,
        "sub_prob": 1.0 / 21.0,
        "red_prob": 1.0 / 21.0,
        "update_prob": 1.0 / 21.0,
    },
}

_VALID_SCENARIOS = ("daily", "peak", "coincident", "smoke")
_active_scenario: str = config.DEFAULT_SCENARIO if config.DEFAULT_SCENARIO in _VALID_SCENARIOS else "daily"
_delay_bounds: tuple[float, float] = (config.STEP_DELAY_MIN, config.STEP_DELAY_MAX)


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Baca konfigurasi scenario, min-delay, dan max-delay saat tes dimulai."""
    global _active_scenario, _delay_bounds
    parsed = environment.parsed_options
    scenario = str(getattr(parsed, "scenario", config.DEFAULT_SCENARIO)).strip().lower()
    _active_scenario = scenario if scenario in _VALID_SCENARIOS else "daily"
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
    """Ambil lastVersionDate terbaru di dalam lock CIF agar tidak basi oleh GET paralel."""
    path = f"{config.CUSTOMER_PREFIX}/v1/customer-personal/byCifBank/{cif}"
    url = f"{config.CUSTOMER_BASE_URL}{path}"
    headers = auth_manager.build_headers("GET", path, None)
    try:
        resp = raw_requests.get(url, headers=headers, timeout=30)
        return _extract_version_from_response(resp)
    except Exception:
        return None


# ==============================================================================
# 3. Helper Endpoint Bersama untuk User Journey
# ==============================================================================
class AvaEndpointMixin:
    """Kumpulan pemanggilan 12 endpoint AVA dengan delay otomatis setelah setiap hit."""

    def _pause_after_hit(self):
        min_d, max_d = _delay_bounds
        if max_d > 0:
            time.sleep(random.uniform(min_d, max_d))

    def _send(self, base_url: str, method: str, path: str, name: str, body: dict | list | None = None, pause: bool = True):
        headers = auth_manager.build_headers(method, path, body)
        url = f"{base_url}{path}"
        kwargs = {"headers": headers, "name": name, "catch_response": True, "timeout": 30}
        if body is not None:
            kwargs["json"] = body
        try:
            with self.client.request(method, url, **kwargs) as resp:
                if getattr(resp, "error", None) is not None or resp.status_code == 0:
                    resp.failure(str(getattr(resp, "error", None) or "Request error / timeout"))
                elif resp.status_code >= 400:
                    snippet = resp.text.replace("\n", " ").strip()[:250]
                    resp.failure(f"HTTP {resp.status_code}: {snippet}")
                else:
                    resp.success()
                return resp
        finally:
            if pause:
                self._pause_after_hit()

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

    def call_create_invaccount(self, cif: str):
        path = f"{config.CUSTOMER_PREFIX}/v1/invaccount/create"
        unique_inv = f"I26{uuid.uuid4().hex[:8].upper()}"
        body = {
            "invAccountNo": unique_inv,
            "invAccountName": f"LoadTest {unique_inv}",
            "cifBank": cif,
        }
        return self._send(config.CUSTOMER_BASE_URL, "POST", path, "POST invaccount/create", body=body)

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
        redeem_unit = config.PRODUCT_MIN_REDEMPTION_UNITS.get(rc["productCode"], config.REDEMPTION_BY_UNIT)
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
                    "redemptionByUnit": redeem_unit,
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
        try:
            with _get_cif_lock(cif):
                version = _fetch_version_quietly(cif)
                if not version:
                    with _version_lock:
                        version = _last_version_cache.pop(cif, None) or session_version

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
                return self._send(config.CUSTOMER_BASE_URL, "PUT", path, "PUT customer-personal/update", body=body, pause=False)
        finally:
            self._pause_after_hit()


# ==============================================================================
# 4. User Journey Realistis dengan Proporsi Sesuai Profil Produksi
# ==============================================================================
class AvaJourneyUser(AvaEndpointMixin, HttpUser):
    host = config.BASE_URL
    # Delay ditangani langsung setelah setiap HTTP hit (_pause_after_hit) agar step yang dilewati tidak ikut sleep.
    wait_time = constant(0)

    def _run_smoke_onboarding(self, c: dict[str, str]):
        self.call_create_customer()
        self.call_create_invaccount(c["cifBank"])
        self.call_post_ut_products(c["cifBank"])
        self.call_get_nav_histories(c["productCode"])
        self.call_subscription(c["cifBank"], c["invAccountNo"], c["productCode"], c["stlAccountNo"])
        self.call_post_order_history(c["cifBank"])

    def _run_smoke_existing(self, c: dict[str, str]):
        self.call_validate_user(c["cifBank"], c["mobileContactNo"], c["birthDate"])
        version = self.call_get_customer_by_cif(c["cifBank"])
        self.call_get_invaccount_by_cif(c["cifBank"])
        self.call_get_balance_latest(c["cifBank"])
        self.call_redemption(random.choice(REDEMPTION_CUSTOMERS))
        self.call_update_customer(c, session_version=version)

    def _run_onboarding_session(self, c: dict[str, str]):
        probe_cif = f"NEW{uuid.uuid4().hex[:9].upper()}"
        probe_mobile = f"+63917{random.randint(1000000, 9999999)}"
        self.call_validate_user(probe_cif, probe_mobile, "1990-05-15")
        self.call_create_customer()
        self.call_create_invaccount(c["cifBank"])
        self.call_get_customer_by_cif(c["cifBank"])
        self.call_post_ut_products(c["cifBank"])
        self.call_subscription(c["cifBank"], c["invAccountNo"], c["productCode"], c["stlAccountNo"])

    def _run_existing_session(self, c: dict[str, str], profile: dict[str, float]):
        cif = c["cifBank"]
        if random.random() < profile["validate_prob"]:
            self.call_validate_user(cif, c["mobileContactNo"], c["birthDate"])

        session_version = self.call_get_customer_by_cif(cif)

        if random.random() < profile["invaccount_by_cif_prob"]:
            self.call_get_invaccount_by_cif(cif)

        if profile.get("extra_invaccount_prob", 0.0) > 0 and random.random() < profile["extra_invaccount_prob"]:
            self.call_create_invaccount(cif)

        if random.random() < profile["balance_latest_prob"]:
            self.call_get_balance_latest(cif)

        if random.random() < profile["order_history_prob"]:
            self.call_post_order_history(cif)

        if random.random() < profile["ut_products_prob"]:
            self.call_post_ut_products(cif)
            if random.random() < profile["nav_given_products_prob"]:
                self.call_get_nav_histories(c["productCode"])

        roll = random.random()
        sub_cut = profile["sub_prob"]
        red_cut = sub_cut + profile["red_prob"]
        upd_cut = red_cut + profile["update_prob"]

        if roll < sub_cut:
            self.call_subscription(cif, c["invAccountNo"], c["productCode"], c["stlAccountNo"])
        elif roll < red_cut:
            self.call_redemption(random.choice(REDEMPTION_CUSTOMERS))
        elif roll < upd_cut:
            self.call_update_customer(c, session_version=session_version)

    @task
    def execute_user_session(self):
        c = random.choice(CUSTOMERS)
        if _active_scenario == "smoke":
            if random.random() < 0.5:
                self._run_smoke_onboarding(c)
            else:
                self._run_smoke_existing(c)
            return

        profile = SCENARIO_PROFILES[_active_scenario]
        if random.random() < profile["onboarding_prob"]:
            self._run_onboarding_session(c)
        else:
            self._run_existing_session(c, profile)
