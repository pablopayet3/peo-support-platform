"""Plan details that come from the carriers' plan documents (2026 plan year), by plan ID.

Kept as reference data rather than database columns: a real PEO receives these from each carrier once a
year. Amounts are in US dollars for in-network care.
"""
PLAN_DETAILS = {
    "PL-01": {"plan_year": 2026, "deductible": {"individual": 500, "family": 1000},
              "out_of_pocket_max": {"individual": 3000, "family": 6000}},
    "PL-02": {"plan_year": 2026, "deductible": {"individual": 1500, "family": 3000},
              "out_of_pocket_max": {"individual": 5000, "family": 10000}},
    "PL-03": {"plan_year": 2026, "deductible": {"individual": 3000, "family": 6000},
              "out_of_pocket_max": {"individual": 7500, "family": 15000}},
}


def details(plan_id: str) -> dict:
    return PLAN_DETAILS.get(plan_id, {"plan_year": None, "deductible": None, "out_of_pocket_max": None})
