import os
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "http://localhost:8083").rstrip("/")
CUSTOMER_BASE_URL = os.getenv("CUSTOMER_BASE_URL", "").strip().rstrip("/") or BASE_URL
UT_BASE_URL = os.getenv("UT_BASE_URL", "").strip().rstrip("/") or BASE_URL
TRANSACTION_BASE_URL = os.getenv("TRANSACTION_BASE_URL", "").strip().rstrip("/") or BASE_URL

CUSTOMER_PREFIX = os.getenv("CUSTOMER_PREFIX", "/customer-service").rstrip("/")
UT_PREFIX = os.getenv("UT_PREFIX", "/ut-service").rstrip("/")
TRANSACTION_PREFIX = os.getenv("TRANSACTION_PREFIX", "/transaction-service").rstrip("/")

ENABLE_AUTH = os.getenv("ENABLE_AUTH", "false").strip().lower() in ("1", "true", "yes", "on")
DEFAULT_SCENARIO = os.getenv("LOAD_SCENARIO", "peak").strip().lower()
DEFAULT_MULTIPLIER = float(os.getenv("LOAD_MULTIPLIER", "0.10"))

LOGIN_ID = os.getenv("LOGIN_ID", "useradmin1")
USER_ID = os.getenv("USER_ID", "585f37d3-6167-4cd6-84a3-17a9f7b2be90")
BRANCH_ID = os.getenv("BRANCH_ID", "b879defe-ee35-4314-9a46-4b87ef6216e7")
DUPLICATE_REF_RATE = float(os.getenv("DUPLICATE_REF_RATE", "0.0"))

TOKEN_PATH = os.getenv("TOKEN_PATH", "/v1.0/access-token/b2b")
CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
PARTNER_ID = os.getenv("PARTNER_ID", "GCASH")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH", "keys/private_key.pem")
TOKEN_REFRESH_SECONDS = int(os.getenv("TOKEN_REFRESH_SECONDS", "800"))

DEFAULT_SALES_ID = os.getenv("DEFAULT_SALES_ID", "46aaeb86-007f-4d72-b346-13ae9b514793")
DEFAULT_BRANCH_ID = os.getenv("DEFAULT_BRANCH_ID", "b879defe-ee35-4314-9a46-4b87ef6216e7")
DEFAULT_BANK_ID = os.getenv("DEFAULT_BANK_ID", "46aaeb86-007f-4d72-b346-13ae9b514793")

# Basis target TPS dari dokumen Section 3: "Peak (p99.9 mix): 61 TPS x share, writes floored" (~66 TPS)
PEAK_P99_9_RATES: dict[str, float] = {
    "customer_by_cif": 22.8,
    "balance_latest": 12.5,
    "order_history": 11.2,
    "ut_products": 7.0,
    "validate_user": 2.8,
    "invaccount_by_cif": 2.5,
    "nav_histories": 1.6,
    "trx_subscription": 2.0,
    "invaccount_create": 1.0,
    "customer_create": 1.0,
    "trx_redemption": 1.0,
    "customer_update": 1.0,
}

# Basis target TPS dari dokumen Section 3: "Coincident peak: each endpoint at its own p99.9" (76 TPS)
COINCIDENT_P99_9_RATES: dict[str, float] = {
    "customer_by_cif": 22.0,
    "balance_latest": 14.0,
    "order_history": 12.0,
    "ut_products": 8.0,
    "validate_user": 5.0,
    "invaccount_by_cif": 5.0,
    "nav_histories": 4.0,
    "trx_subscription": 2.0,
    "invaccount_create": 1.0,
    "customer_create": 1.0,
    "trx_redemption": 1.0,
    "customer_update": 1.0,
}

# Smoke test (S1): 1 TPS per endpoint (12 TPS total sebelum multiplier)
SMOKE_RATES: dict[str, float] = {key: 1.0 for key in PEAK_P99_9_RATES}


def get_target_rates(scenario: str, multiplier: float) -> dict[str, float]:
    """Hitung target TPS tiap endpoint berdasarkan skenario dan multiplier."""
    scenario_key = (scenario or "peak").strip().lower()
    if scenario_key == "coincident":
        base = COINCIDENT_P99_9_RATES
    elif scenario_key == "smoke":
        base = SMOKE_RATES
    else:
        base = PEAK_P99_9_RATES

    factor = max(0.0001, float(multiplier))
    return {endpoint: round(tps * factor, 4) for endpoint, tps in base.items()}
