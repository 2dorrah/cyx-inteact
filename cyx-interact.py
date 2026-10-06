#!/usr/bin/env python3
"""
CYTHANX National Debt + Spending Trend + Money Demand Solver
==============================================================

A standalone fiscal simulation tool that combines:

  1. Annual/monthly CSV ingestion
  2. Historical spending/revenue trend discovery
  3. Annual spending funding-demand calculation
  4. Debt and interest simulation
  5. Spending/revenue policy optimization
  6. One-command integrated CYTHANX + fiscal-data pipeline

IMPORTANT:
This calculator models funding requirements. It does NOT create money,
mint currency, access banking systems, or execute financial transactions.

Core identities:
    Primary deficit = Spending - Revenue
    Funding gap     = max(0, Spending - Revenue)
    Interest        = Debt * InterestRate
    Total deficit   = Primary deficit + Interest
    Debt[t+1]       = Debt[t] + Total deficit

All monetary inputs are billions of dollars unless otherwise stated.

CSV:
    Required: period,revenue,spending
    Optional: gdp,debt

Annual CSV example:
    period,revenue,spending,gdp,debt
    2021,4000,5200,23000,28000
    2022,4300,5500,24500,29500

Monthly CSV example:
    period,revenue,spending,gdp,debt
    2025-01,410,540,30000,35000
    2025-02,420,550,30100,35200

For monthly data, revenue/spending are summed by calendar year while
available GDP/debt observations are averaged/latest-observation based
on their respective meaning.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


# ============================================================
# FOREIGN CURRENCY -> USD NORMALIZATION LAYER
# ============================================================
# All fiscal/crypto values entering the unified calculation can
# be normalized to U.S. dollars first. Rates are explicit inputs
# so the calculation remains auditable and does not silently
# substitute an exchange rate.

DEFAULT_USD_RATES = {
    "USD": 1.0,
    "EUR": 1.0,
    "GBP": 1.0,
    "JPY": 1.0,
    "CNY": 1.0,
    "CAD": 1.0,
    "AUD": 1.0,
    "CHF": 1.0,
    "INR": 1.0,
    "MXN": 1.0,
    "BRL": 1.0,
    "KRW": 1.0,
}

def normalize_currency(amount: float, currency: str, usd_rate: float) -> float:
    """Convert an amount in foreign currency into USD.

    usd_rate means: 1 unit of `currency` = usd_rate USD.
    """
    if amount < 0:
        raise ValueError("currency amount cannot be negative")
    if usd_rate <= 0:
        raise ValueError("USD conversion rate must be greater than zero")
    return amount * usd_rate

def normalize_to_usd(
    values: dict[str, float],
    rates: dict[str, float],
) -> dict[str, float]:
    """Convert a basket of currency amounts to USD."""
    result = {}
    for currency, amount in values.items():
        code = currency.upper()
        if code not in rates:
            raise ValueError(f"missing USD exchange rate for {code}")
        result[code] = normalize_currency(amount, code, rates[code])
    return result

# ============================================================
# PRINTABLE MONEY / CRYPTO DEBT EQUIVALENCE LAYER
# ============================================================
# "Printable money" is a hypothetical fiat-equivalent accounting
# value, not actual money creation or a claim that crypto can
# automatically extinguish public debt.

CRYPTO_DEFAULTS = {
    "BTC": {"price": 86281.11, "supply": 21_000_000.0},
}

def crypto_value(price: float, supply: float) -> float:
    if price < 0 or supply < 0:
        raise ValueError("crypto price and supply cannot be negative")
    return price * supply

def printable_money_equivalent(
    debt: float,
    crypto_values: dict[str, float],
    monetization_fraction: float = 1.0,
) -> dict:
    if debt < 0:
        raise ValueError("debt cannot be negative")
    if not 0.0 <= monetization_fraction <= 1.0:
        raise ValueError("fraction must be between 0 and 1")
    total = sum(max(0.0, v) for v in crypto_values.values())
    equivalent = total * monetization_fraction
    covered = min(debt, equivalent)
    return {
        "debt": debt,
        "crypto_market_value": total,
        "monetization_fraction": monetization_fraction,
        "printable_money_equivalent": equivalent,
        "debt_covered": covered,
        "uncovered_debt": max(0.0, debt - equivalent),
        "coverage_ratio": equivalent / debt if debt else 1.0,
    }

def print_printable_money_report(debt, crypto_values, fraction):
    r = printable_money_equivalent(debt, crypto_values, fraction)
    print("\nCRYPTO -> PRINTABLE MONEY -> NATIONAL DEBT")
    print("=" * 76)
    print(f"National debt:                 {money(r['debt'])}")
    print(f"Crypto market-value basket:    {money(r['crypto_market_value'])}")
    print(f"Modeled conversion fraction:   {fraction:.2%}")
    print(f"Hypothetical fiat equivalent:  {money(r['printable_money_equivalent'])}")
    print(f"Debt covered:                  {money(r['debt_covered'])}")
    print(f"Debt remaining:                {money(r['uncovered_debt'])}")
    print(f"Debt coverage ratio:           {r['coverage_ratio']:.2%}")
    print("\nThis is a valuation/accounting scenario, not actual money creation.")



APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "cythanx-debt-config.json"


@dataclass(frozen=True)
class FiscalConfig:
    years: int = 10
    starting_debt: float = 36000.0
    starting_gdp: float = 30000.0
    starting_revenue: float = 5000.0
    starting_spending: float = 6500.0
    interest_rate: float = 0.035
    gdp_growth: float = 0.025
    revenue_growth: float = 0.025
    spending_growth: float = 0.025
    spending_reduction: float = 0.0
    revenue_increase: float = 0.0
    debt_weight: float = 1.0
    debt_ratio_weight: float = 2.0
    interest_weight: float = 0.5
    policy_cost_weight: float = 0.25
    # Bitcoin monetary-reference layer
    starting_btc_price: float = 86_281.11
    bitcoin_price_growth: float = 0.05
    bitcoin_supply_cap: float = 21_000_000.0


@dataclass(frozen=True)
class FiscalObservation:
    period: str
    revenue: float
    spending: float
    gdp: float | None = None
    debt: float | None = None


@dataclass(frozen=True)
class TrendMetrics:
    periods: int
    revenue_growth: float
    spending_growth: float
    gdp_growth: float | None
    debt_growth: float | None
    spending_to_revenue: float
    spending_to_gdp: float | None
    revenue_to_gdp: float | None
    primary_deficit: float
    spending_acceleration: float
    revenue_acceleration: float
    debt_acceleration: float | None
    risk: str


@dataclass(frozen=True)
class DemandMetrics:
    annual_spending: float
    available_revenue: float
    primary_funding_gap: float
    spending_coverage: float
    additional_revenue_for_balance: float
    interest_cost: float
    total_funding_requirement: float
    additional_funding_after_interest: float
    debt_financing_share: float


@dataclass(frozen=True)
class YearResult:
    year: int
    debt: float
    gdp: float
    revenue: float
    spending: float
    primary_deficit: float
    interest: float
    total_deficit: float
    debt_to_gdp: float
    spending_coverage: float
    btc_price: float
    debt_btc: float
    debt_to_btc_supply: float
    implied_btc_price_full_supply: float


def safe_growth(first: float, last: float, periods: int) -> float:
    if periods <= 0 or first <= 0 or last < 0:
        return 0.0
    return (last / first) ** (1.0 / periods) - 1.0


def validate_config(c: FiscalConfig) -> list[str]:
    errors: list[str] = []
    if not 1 <= c.years <= 200:
        errors.append("years must be between 1 and 200")
    for name in (
        "starting_debt", "starting_gdp", "starting_revenue",
        "starting_spending",
    ):
        if getattr(c, name) < 0:
            errors.append(f"{name} cannot be negative")
    if c.starting_gdp <= 0:
        errors.append("starting_gdp must be greater than zero")
    if c.interest_rate < -1:
        errors.append("interest_rate cannot be below -100%")
    for name in ("gdp_growth", "revenue_growth", "spending_growth"):
        if getattr(c, name) < -1:
            errors.append(f"{name} cannot be below -100%")
    if c.spending_reduction < 0:
        errors.append("spending_reduction cannot be negative")
    if c.revenue_increase < 0:
        errors.append("revenue_increase cannot be negative")
    if c.starting_btc_price <= 0:
        errors.append("starting_btc_price must be greater than zero")
    if c.bitcoin_supply_cap <= 0:
        errors.append("bitcoin_supply_cap must be greater than zero")
    if c.bitcoin_price_growth < -1:
        errors.append("bitcoin_price_growth cannot be below -100%")
    return errors


def simulate(c: FiscalConfig) -> list[YearResult]:
    errors = validate_config(c)
    if errors:
        raise ValueError("; ".join(errors))

    debt = c.starting_debt
    gdp = c.starting_gdp
    revenue = c.starting_revenue
    spending = c.starting_spending
    btc_price = c.starting_btc_price
    results: list[YearResult] = []

    for year in range(1, c.years + 1):
        gdp *= 1.0 + c.gdp_growth
        revenue *= max(0.0, 1.0 + c.revenue_growth + c.revenue_increase)
        spending *= max(0.0, 1.0 + c.spending_growth - c.spending_reduction)
        btc_price *= max(0.0, 1.0 + c.bitcoin_price_growth)

        interest = debt * c.interest_rate
        primary_deficit = spending - revenue
        total_deficit = primary_deficit + interest
        debt += total_deficit

        coverage = revenue / spending if spending > 0 else 1.0
        debt_btc = debt * 1_000_000_000 / btc_price
        debt_to_btc_supply = debt_btc / c.bitcoin_supply_cap
        implied_btc_price = debt * 1_000_000_000 / c.bitcoin_supply_cap

        results.append(
            YearResult(
                year=year,
                debt=debt,
                gdp=gdp,
                revenue=revenue,
                spending=spending,
                primary_deficit=primary_deficit,
                interest=interest,
                total_deficit=total_deficit,
                debt_to_gdp=debt / gdp,
                spending_coverage=coverage,
                btc_price=btc_price,
                debt_btc=debt_btc,
                debt_to_btc_supply=debt_to_btc_supply,
                implied_btc_price_full_supply=implied_btc_price,
            )
        )
    return results


def money(x: float) -> str:
    return f"${x:,.2f}B"


def demand_calculator(
    spending: float,
    revenue: float,
    debt: float = 0.0,
    interest_rate: float = 0.0,
) -> DemandMetrics:
    if spending < 0 or revenue < 0 or debt < 0:
        raise ValueError("spending, revenue, and debt must be non-negative")

    primary_gap = max(0.0, spending - revenue)
    coverage = revenue / spending if spending > 0 else 1.0
    additional_balance = primary_gap
    interest = debt * interest_rate

    # Amount that would have to be funded externally to cover both the
    # primary spending gap and interest obligation.
    total_requirement = primary_gap + interest
    additional_after_interest = max(0.0, total_requirement)
    debt_share = (
        total_requirement / spending if spending > 0 else 0.0
    )

    return DemandMetrics(
        annual_spending=spending,
        available_revenue=revenue,
        primary_funding_gap=primary_gap,
        spending_coverage=coverage,
        additional_revenue_for_balance=additional_balance,
        interest_cost=interest,
        total_funding_requirement=total_requirement,
        additional_funding_after_interest=additional_after_interest,
        debt_financing_share=debt_share,
    )


def scenario_score(c: FiscalConfig, results: list[YearResult]) -> float:
    final = results[-1]
    avg_ratio = statistics.fmean(r.debt_to_gdp for r in results)
    total_interest = sum(r.interest for r in results)
    policy_cost = c.spending_reduction**2 + c.revenue_increase**2

    return (
        c.debt_weight * final.debt
        + c.debt_ratio_weight * final.debt_to_gdp * c.starting_gdp
        + c.interest_weight * total_interest
        + c.policy_cost_weight * policy_cost * c.starting_gdp
        + 0.25 * avg_ratio * c.starting_gdp
    )


def fingerprint(c: FiscalConfig) -> str:
    canonical = json.dumps(asdict(c), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def parse_float(value: str, field: str, rownum: int) -> float:
    try:
        return float(value.replace(",", "").replace("$", "").strip())
    except ValueError as exc:
        raise ValueError(
            f"CSV row {rownum}: invalid {field}: {value!r}"
        ) from exc


def read_csv(path: Path) -> list[FiscalObservation]:
    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError("CSV has no header")

        fields = {x.strip().lower(): x for x in reader.fieldnames}
        required = {"period", "revenue", "spending"}
        missing = required - fields.keys()
        if missing:
            raise ValueError(
                "CSV is missing required columns: "
                + ", ".join(sorted(missing))
            )

        observations: list[FiscalObservation] = []

        for rownum, row in enumerate(reader, 2):
            period = row[fields["period"]].strip()
            if not period:
                continue

            revenue = parse_float(row[fields["revenue"]], "revenue", rownum)
            spending = parse_float(row[fields["spending"]], "spending", rownum)

            if revenue < 0 or spending < 0:
                raise ValueError(
                    f"CSV row {rownum}: revenue/spending cannot be negative"
                )

            gdp = None
            debt = None
            if "gdp" in fields and row[fields["gdp"]].strip():
                gdp = parse_float(row[fields["gdp"]], "gdp", rownum)
            if "debt" in fields and row[fields["debt"]].strip():
                debt = parse_float(row[fields["debt"]], "debt", rownum)

            observations.append(
                FiscalObservation(period, revenue, spending, gdp, debt)
            )

    if len(observations) < 2:
        raise ValueError("At least two CSV observations are required")
    return observations


def period_year(period: str) -> int:
    try:
        return int(period[:4])
    except ValueError as exc:
        raise ValueError(
            f"Cannot determine year from period {period!r}"
        ) from exc


def looks_monthly(observations: list[FiscalObservation]) -> bool:
    return any("-" in x.period[:7] for x in observations)


def aggregate_annual(
    observations: list[FiscalObservation],
) -> list[FiscalObservation]:
    buckets: dict[int, list[FiscalObservation]] = {}

    for obs in observations:
        buckets.setdefault(period_year(obs.period), []).append(obs)

    annual: list[FiscalObservation] = []

    for year in sorted(buckets):
        rows = buckets[year]

        # Revenue and spending are flow variables.
        revenue = sum(x.revenue for x in rows)
        spending = sum(x.spending for x in rows)

        gdp_values = [x.gdp for x in rows if x.gdp is not None]
        debt_values = [x.debt for x in rows if x.debt is not None]

        annual.append(
            FiscalObservation(
                period=str(year),
                revenue=revenue,
                spending=spending,
                gdp=statistics.fmean(gdp_values)
                if gdp_values else None,
                # Debt is a stock: use the latest supplied observation.
                debt=debt_values[-1] if debt_values else None,
            )
        )

    return annual


def discover_trends(
    observations: list[FiscalObservation],
) -> TrendMetrics:
    annual = aggregate_annual(observations)

    first = annual[0]
    last = annual[-1]
    n = len(annual) - 1

    revenue_growth = safe_growth(first.revenue, last.revenue, n)
    spending_growth = safe_growth(first.spending, last.spending, n)

    gdp_values = [x.gdp for x in annual if x.gdp is not None]
    debt_values = [x.debt for x in annual if x.debt is not None]

    gdp_growth = (
        safe_growth(gdp_values[0], gdp_values[-1], len(gdp_values) - 1)
        if len(gdp_values) >= 2 else None
    )

    debt_growth = (
        safe_growth(debt_values[0], debt_values[-1], len(debt_values) - 1)
        if len(debt_values) >= 2 else None
    )

    spending_rates = [
        safe_growth(
            annual[i - 1].spending,
            annual[i].spending,
            1,
        )
        for i in range(1, len(annual))
    ]

    revenue_rates = [
        safe_growth(
            annual[i - 1].revenue,
            annual[i].revenue,
            1,
        )
        for i in range(1, len(annual))
    ]

    debt_rates = [
        safe_growth(
            annual[i - 1].debt,
            annual[i].debt,
            1,
        )
        for i in range(1, len(annual))
        if annual[i - 1].debt is not None
        and annual[i].debt is not None
    ]

    def acceleration(rates: list[float]) -> float:
        if len(rates) < 2:
            return 0.0
        return rates[-1] - statistics.fmean(rates[:-1])

    spending_accel = acceleration(spending_rates)
    revenue_accel = acceleration(revenue_rates)
    debt_accel = acceleration(debt_rates) if debt_rates else None

    spending_to_revenue = (
        last.spending / last.revenue
        if last.revenue else math.inf
    )

    spending_to_gdp = (
        last.spending / last.gdp
        if last.gdp else None
    )

    revenue_to_gdp = (
        last.revenue / last.gdp
        if last.gdp else None
    )

    primary_deficit = last.spending - last.revenue

    if spending_accel > 0.02 and spending_growth > revenue_growth:
        risk = "DETERIORATING"
    elif spending_to_revenue > 1.25 and spending_growth > revenue_growth:
        risk = "PRIMARY-DEFICIT DOMINATED"
    elif (
        debt_accel is not None
        and debt_growth is not None
        and debt_growth > 0.08
    ):
        risk = "DEBT-SPIRAL RISK"
    elif spending_growth < revenue_growth and primary_deficit <= 0:
        risk = "IMPROVING"
    else:
        risk = "STABLE / WATCH"

    return TrendMetrics(
        periods=len(annual),
        revenue_growth=revenue_growth,
        spending_growth=spending_growth,
        gdp_growth=gdp_growth,
        debt_growth=debt_growth,
        spending_to_revenue=spending_to_revenue,
        spending_to_gdp=spending_to_gdp,
        revenue_to_gdp=revenue_to_gdp,
        primary_deficit=primary_deficit,
        spending_acceleration=spending_accel,
        revenue_acceleration=revenue_accel,
        debt_acceleration=debt_accel,
        risk=risk,
    )


def data_informed_config(
    base: FiscalConfig,
    observations: list[FiscalObservation],
) -> tuple[FiscalConfig, TrendMetrics]:
    annual = aggregate_annual(observations)
    trend = discover_trends(observations)
    last = annual[-1]

    starting_debt = (
        last.debt if last.debt is not None
        else base.starting_debt
    )

    starting_gdp = (
        last.gdp if last.gdp is not None
        else base.starting_gdp
    )

    informed = FiscalConfig(
        **{
            **asdict(base),
            "starting_debt": starting_debt,
            "starting_gdp": starting_gdp,
            "starting_revenue": last.revenue,
            "starting_spending": last.spending,
            "revenue_growth": trend.revenue_growth,
            "spending_growth": trend.spending_growth,
            "gdp_growth": (
                trend.gdp_growth
                if trend.gdp_growth is not None
                else base.gdp_growth
            ),
        }
    )

    return informed, trend


def optimize(
    base: FiscalConfig,
    spending_grid: Iterable[float],
    revenue_grid: Iterable[float],
) -> tuple[FiscalConfig, list[YearResult], float]:
    best = None

    for spending_reduction, revenue_increase in itertools.product(
        spending_grid,
        revenue_grid,
    ):
        candidate = FiscalConfig(
            **{
                **asdict(base),
                "spending_reduction": spending_reduction,
                "revenue_increase": revenue_increase,
            }
        )

        results = simulate(candidate)
        score = scenario_score(candidate, results)

        if best is None or score < best[2]:
            best = (candidate, results, score)

    assert best is not None
    return best


def make_grid(maximum: float, steps: int) -> list[float]:
    return [
        maximum * i / (steps - 1)
        for i in range(steps)
    ]


def save_config(c: FiscalConfig, path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "config": asdict(c),
                "fingerprint": fingerprint(c),
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def load_config(path: Path) -> FiscalConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return FiscalConfig(**raw.get("config", raw))


def load_base_config(path: Path) -> FiscalConfig:
    if not path.exists():
        c = FiscalConfig()
        save_config(c, path)
        return c
    return load_config(path)


def print_demand(d: DemandMetrics) -> None:
    print("\nANNUAL MONEY / FUNDING DEMAND")
    print("=" * 76)
    print(f"Annual spending demand:          {money(d.annual_spending)}")
    print(f"Available revenue:                {money(d.available_revenue)}")
    print(f"Spending coverage:                {d.spending_coverage:.2%}")
    print(f"Primary funding gap:              {money(d.primary_funding_gap)}")
    print(f"Revenue needed for balance:      {money(d.additional_revenue_for_balance)}")
    print(f"Interest obligation:              {money(d.interest_cost)}")
    print(f"Total funding requirement:        {money(d.total_funding_requirement)}")
    print(f"Debt-financing share of spending: {d.debt_financing_share:.2%}")


def print_trends(t: TrendMetrics) -> None:
    print("\nDISCOVERED FISCAL TRENDS")
    print("=" * 76)
    print(f"Historical annual periods: {t.periods}")
    print(f"Spending CAGR:             {t.spending_growth:.2%}")
    print(f"Revenue CAGR:              {t.revenue_growth:.2%}")

    if t.gdp_growth is not None:
        print(f"GDP CAGR:                  {t.gdp_growth:.2%}")

    if t.debt_growth is not None:
        print(f"Debt CAGR:                 {t.debt_growth:.2%}")

    print(f"Spending / Revenue:        {t.spending_to_revenue:.2%}")

    if t.spending_to_gdp is not None:
        print(f"Spending / GDP:            {t.spending_to_gdp:.2%}")

    if t.revenue_to_gdp is not None:
        print(f"Revenue / GDP:             {t.revenue_to_gdp:.2%}")

    print(f"Primary deficit:           {money(t.primary_deficit)}")
    print(f"Spending acceleration:     {t.spending_acceleration:+.2%}")
    print(f"Revenue acceleration:      {t.revenue_acceleration:+.2%}")

    if t.debt_acceleration is not None:
        print(f"Debt acceleration:         {t.debt_acceleration:+.2%}")

    print(f"Trend classification:      {t.risk}")


def print_results(
    c: FiscalConfig,
    results: list[YearResult],
) -> None:
    print("\nCYTHANX NATIONAL DEBT SOLVER")
    print("=" * 76)
    print(f"Scenario fingerprint: {fingerprint(c)}")
    print(f"Years:                 {c.years}")
    print(f"Starting debt:         {money(c.starting_debt)}")
    print(f"Starting GDP:          {money(c.starting_gdp)}")
    print(f"Interest rate:         {c.interest_rate:.2%}")
    print(f"Spending reduction:    {c.spending_reduction:.2%}/yr")
    print(f"Revenue increase:      {c.revenue_increase:.2%}/yr")
    print(f"Starting BTC price:    ${c.starting_btc_price:,.2f}")
    print(f"BTC price growth:      {c.bitcoin_price_growth:.2%}/yr")
    print(f"BTC supply cap:        {c.bitcoin_supply_cap:,.0f}")
    print()

    header = (
        f"{'Year':>4} {'Debt':>14} {'GDP':>14} "
        f"{'Revenue':>14} {'Spend.':>14} {'Coverage':>10} "
        f"{'Debt/GDP':>10} {'BTC/USD':>14} {'Debt/BTC':>16} {'Debt/21M':>10}"
    )

    print(header)
    print("-" * len(header))

    for r in results:
        print(
            f"{r.year:>4} "
            f"{money(r.debt):>14} "
            f"{money(r.gdp):>14} "
            f"{money(r.revenue):>14} "
            f"{money(r.spending):>14} "
            f"{r.spending_coverage:>9.1%} "
            f"{r.debt_to_gdp:>9.1%} "
            f"${r.btc_price:>13,.0f} "
            f"{r.debt_btc:>16,.0f} "
            f"{r.debt_to_btc_supply:>9.1%}"
        )

    final = results[-1]

    print("\nFINAL ASSESSMENT")
    print("-" * 76)
    print(f"Final debt:            {money(final.debt)}")
    print(f"Final debt/GDP:        {final.debt_to_gdp:.2%}")
    print(f"Final primary deficit: {money(final.primary_deficit)}")
    print(f"Final interest:        {money(final.interest)}")
    print(f"Total modeled interest:{money(sum(r.interest for r in results))}")
    print(f"Final BTC price:       ${final.btc_price:,.2f}")
    print(f"Final debt in BTC:     {final.debt_btc:,.0f} BTC")
    print(f"Final debt / 21M BTC:  {final.debt_to_btc_supply:.2%}")
    print(f"BTC price for debt=21M: ${final.implied_btc_price_full_supply:,.2f} per BTC")



@dataclass(frozen=True)
class BitcoinEconomics:
    """Global-debt valuation bridge expressed in BTC terms."""
    global_debt: float = 365_000.0
    global_gdp: float = 126_300.0
    bitcoin_price: float = 86_281.11
    bitcoin_supply_cap: float = 21_000_000.0

    @property
    def debt_to_gdp(self):
        return self.global_debt / self.global_gdp if self.global_gdp else float("inf")
    @property
    def btc_required_at_market_price(self):
        return self.global_debt * 1_000_000_000 / self.bitcoin_price
    @property
    def implied_btc_price_for_full_supply(self):
        return self.global_debt * 1_000_000_000 / self.bitcoin_supply_cap
    @property
    def debt_per_capita(self):
        return self.global_debt * 1_000_000_000 / 8.05e9
    @property
    def debt_in_sat(self):
        return self.global_debt * 1_000_000_000 * 100_000_000 / self.bitcoin_price

def print_bitcoin_economics(b):
    print("\nCYTHANX GLOBAL DEBT ↔ BITCOIN ECONOMICS")
    print("=" * 76)
    print(f"Global debt:                         {money(b.global_debt)}")
    print(f"Global nominal GDP:                  {money(b.global_gdp)}")
    print(f"Debt / global GDP:                   {b.debt_to_gdp:.2%}")
    print(f"Bitcoin reference price:             ${b.bitcoin_price:,.2f}/BTC")
    print(f"BTC supply cap:                      {b.bitcoin_supply_cap:,.0f} BTC")
    print(f"BTC needed to equal global debt:     {b.btc_required_at_market_price:,.0f} BTC")
    print(f"Implied BTC price if 21M BTC = debt: ${b.implied_btc_price_for_full_supply:,.2f}")
    print(f"Global debt per person (~8.05B):     ${b.debt_per_capita:,.2f}")
    print(f"Debt expressed at market price:      {b.debt_in_sat:,.0f} satoshis")
    print("\nInterpretation:")
    print("  • Valuation conversion only; Bitcoin does not automatically repay world debt.")
    print("  • Global debt here is the IIF-style public + private debt aggregate.")


def _prompt_float(label, default):
    raw = input(f"{label} [{default}]: ").strip()
    return default if not raw else float(raw)


def _prompt_int(label, default):
    raw = input(f"{label} [{default}]: ").strip()
    return default if not raw else int(raw)


def _load_interactive_config():
    path = Path(input(f"Config path [{DEFAULT_CONFIG}]: ").strip() or DEFAULT_CONFIG)
    return path.expanduser().resolve(), load_base_config(path)


def interactive_init():
    path = Path(input(f"Config path [{DEFAULT_CONFIG}]: ").strip() or DEFAULT_CONFIG)
    path = path.expanduser().resolve()
    config = FiscalConfig()
    save_config(config, path)
    print(f"Created {path}")
    print(f"Fingerprint: {fingerprint(config)}")


def interactive_simulate():
    path, base = _load_interactive_config()
    btc_price = _prompt_float("Starting BTC/USD price", base.starting_btc_price)
    btc_growth = _prompt_float("Annual BTC price growth", base.bitcoin_price_growth)

    config = FiscalConfig(**{
        **asdict(base),
        "starting_btc_price": btc_price,
        "bitcoin_price_growth": btc_growth,
    })
    print_results(config, simulate(config))


def interactive_demand():
    spending = _prompt_float("Annual spending", 6500.0)
    revenue = _prompt_float("Annual revenue", 5000.0)
    debt = _prompt_float("Debt", 36000.0)
    interest_rate = _prompt_float("Interest rate", 0.035)
    print_demand(demand_calculator(spending, revenue, debt, interest_rate))


def interactive_bitcoin_economics():
    debt = _prompt_float("Global debt (USD billions)", 365000.0)
    gdp = _prompt_float("Global nominal GDP (USD billions)", 126300.0)
    price = _prompt_float("BTC/USD reference price", 86281.11)
    cap = _prompt_float("Bitcoin supply cap", 21000000.0)

    b = BitcoinEconomics(debt, gdp, price, cap)
    if debt < 0 or gdp <= 0 or price <= 0 or cap <= 0:
        raise ValueError(
            "global debt must be non-negative; GDP, BTC price, and supply cap "
            "must be positive"
        )
    print_bitcoin_economics(b)


def interactive_analyze():
    path, base = _load_interactive_config()
    csv_path = Path(input("CSV file: ").strip()).expanduser().resolve()

    observations = read_csv(csv_path)
    informed, trends = data_informed_config(base, observations)
    annual = aggregate_annual(observations)
    latest = annual[-1]

    print(f"\nCSV: {csv_path}")
    print(f"Observations: {len(observations)}")
    print(
        "Detected frequency:",
        "monthly/irregular" if looks_monthly(observations) else "annual",
    )
    print_trends(trends)

    demand = demand_calculator(
        latest.spending,
        latest.revenue,
        informed.starting_debt,
        informed.interest_rate,
    )
    print_demand(demand)

    print("\nDATA-INFORMED BASELINE")
    print("=" * 76)
    print(f"Starting debt:         {money(informed.starting_debt)}")
    print(f"Starting GDP:          {money(informed.starting_gdp)}")
    print(f"Starting revenue:      {money(informed.starting_revenue)}")
    print(f"Starting spending:     {money(informed.starting_spending)}")
    print(f"Spending growth:       {informed.spending_growth:.2%}")
    print(f"Revenue growth:        {informed.revenue_growth:.2%}")
    print(f"GDP growth:            {informed.gdp_growth:.2%}")


def interactive_optimize():
    path, base = _load_interactive_config()
    csv_path = Path(input("CSV file: ").strip()).expanduser().resolve()

    observations = read_csv(csv_path)
    informed, trends = data_informed_config(base, observations)

    spending_max = _prompt_float("Maximum annual spending reduction", 0.05)
    revenue_max = _prompt_float("Maximum annual revenue increase", 0.05)
    steps = _prompt_int("Optimization grid steps", 11)

    if not 2 <= steps <= 101:
        raise ValueError("optimization steps must be between 2 and 101")
    if spending_max < 0 or revenue_max < 0:
        raise ValueError("policy maxima cannot be negative")

    spending_grid = make_grid(spending_max, steps)
    revenue_grid = make_grid(revenue_max, steps)
    best_c, results, score = optimize(informed, spending_grid, revenue_grid)

    print("\nBEST DATA-INFORMED MODELED SCENARIO")
    print("=" * 76)
    print(f"Objective score:       {score:,.4f}")
    print(f"Spending reduction:    {best_c.spending_reduction:.2%}/yr")
    print(f"Revenue increase:      {best_c.revenue_increase:.2%}/yr")
    print_results(best_c, results)


def interactive_printable_money():
    debt = _prompt_float("Debt (USD billions)", 36000.0)
    fraction = _prompt_float("Monetization fraction", 1.0)

    assets = {}
    print("\nEnter crypto assets. Press Enter for symbol to finish.")
    while True:
        symbol = input("Asset symbol: ").strip()
        if not symbol:
            break
        price = _prompt_float(f"{symbol} price (USD)", 0.0)
        supply = _prompt_float(f"{symbol} supply", 0.0)
        assets[symbol.upper()] = crypto_value(price, supply)

    if not assets:
        assets = {
            symbol: crypto_value(v["price"], v["supply"])
            for symbol, v in CRYPTO_DEFAULTS.items()
        }

    print("\nCRYPTO ASSET VALUES")
    print("=" * 76)
    for symbol, value in assets.items():
        print(f"{symbol:<8} {money(value):>20}")

    print_printable_money_report(debt, assets, fraction)


def interactive_fx():
    basket = {}
    rates = {}

    print("\nEnter currency amounts. Press Enter for currency to finish.")
    while True:
        currency = input("Currency: ").strip()
        if not currency:
            break
        amount = _prompt_float(f"{currency} amount", 0.0)
        rate = _prompt_float(f"{currency} USD per unit", 1.0)
        code = currency.upper()
        basket[code] = amount
        rates[code] = rate

    converted = normalize_to_usd(basket, rates)

    print("\nFOREIGN CURRENCY -> USD")
    print("=" * 76)
    total = 0.0
    for code, usd in converted.items():
        total += usd
        print(
            f"{code:<8} original={basket[code]:,.2f}  "
            f"rate={rates[code]:,.8f} USD/unit  USD={usd:,.2f}"
        )
    print("-" * 76)
    print(f"TOTAL USD: {total:,.2f}")


def interactive_all():
    path, base = _load_interactive_config()
    csv_path = Path(input("CSV file: ").strip()).expanduser().resolve()

    observations = read_csv(csv_path)
    informed, trends = data_informed_config(base, observations)
    annual = aggregate_annual(observations)
    latest = annual[-1]

    years = _prompt_int("Projected years", informed.years)
    spending_max = _prompt_float("Maximum annual spending reduction", 0.05)
    revenue_max = _prompt_float("Maximum annual revenue increase", 0.05)
    steps = _prompt_int("Optimization grid steps", 11)
    btc_price = _prompt_float(
        "Starting BTC/USD reference price", informed.starting_btc_price
    )
    btc_growth = _prompt_float(
        "Annual BTC price growth", informed.bitcoin_price_growth
    )

    if not 1 <= years <= 200:
        raise ValueError("years must be between 1 and 200")
    if not 2 <= steps <= 101:
        raise ValueError("optimization steps must be between 2 and 101")
    if spending_max < 0 or revenue_max < 0:
        raise ValueError("policy maxima cannot be negative")

    informed = FiscalConfig(**{
        **asdict(informed),
        "years": years,
        "starting_btc_price": btc_price,
        "bitcoin_price_growth": btc_growth,
    })

    demand = demand_calculator(
        latest.spending,
        latest.revenue,
        informed.starting_debt,
        informed.interest_rate,
    )

    print("\n" + "=" * 76)
    print("CYTHANX + FISCAL DATA INTEGRATION")
    print("Mode: INTERACTIVE ALL-IN-ONE")
    print("=" * 76)

    print("\n1. HISTORICAL FISCAL DATA")
    print("-" * 76)
    print(f"Latest annual period:   {latest.period}")
    print(f"Latest spending:        {money(latest.spending)}")
    print(f"Latest revenue:         {money(latest.revenue)}")
    if latest.gdp is not None:
        print(f"Latest GDP:             {money(latest.gdp)}")
    if latest.debt is not None:
        print(f"Latest debt:            {money(latest.debt)}")
    print_trends(trends)
    print_demand(demand)

    print("\n2. CYTHANX DATA-INFORMED PROJECTION")
    print("-" * 76)
    baseline_results = simulate(informed)
    print_results(informed, baseline_results)

    spending_grid = make_grid(spending_max, steps)
    revenue_grid = make_grid(revenue_max, steps)
    best_c, best_results, score = optimize(
        informed, spending_grid, revenue_grid
    )

    print("\n3. CYTHANX POLICY OPTIMIZATION")
    print("-" * 76)
    print(f"Objective score:       {score:,.4f}")
    print(f"Spending reduction:    {best_c.spending_reduction:.2%}/yr")
    print(f"Revenue increase:      {best_c.revenue_increase:.2%}/yr")
    print_results(best_c, best_results)

    summary = {
        "historical_periods": len(observations),
        "latest_period": latest.period,
        "trend": asdict(trends),
        "demand": asdict(demand),
        "baseline": {
            "config": asdict(informed),
            "final": asdict(baseline_results[-1]),
        },
        "optimized": {
            "config": asdict(best_c),
            "score": score,
            "final": asdict(best_results[-1]),
        },
    }

    summary_path = csv_path.with_suffix(".cythanx-summary.json")
    summary_path.write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\nMachine-readable summary: {summary_path}")


COMMANDS = {
    "init": interactive_init,
    "simulate": interactive_simulate,
    "demand": interactive_demand,
    "bitcoin-economics": interactive_bitcoin_economics,
    "analyze": interactive_analyze,
    "optimize": interactive_optimize,
    "all": interactive_all,
    "combined": interactive_all,
    "printable-money": interactive_printable_money,
    "fx-to-usd": interactive_fx,
}


def print_help():
    print("""
Available CYTHANX commands:

  init                Create a default configuration
  simulate            Run the configured fiscal projection
  demand              Calculate annual funding demand
  bitcoin-economics   Show global-debt/BTC valuation reference
  analyze             Analyze annual/monthly CSV trends
  optimize            Optimize modeled fiscal policy
  all                 Run the complete integrated pipeline
  combined            Alias for all
  printable-money     Model crypto value as a hypothetical fiat equivalent
  fx-to-usd           Convert entered foreign-currency values to USD
  help                Show this menu
  clear               Clear the screen
  quit / exit         Leave the program

Commands are entered directly at the CYTHANX prompt.
""")


def main() -> int:
    print("""
============================================================
                    CYTHANX CONSOLE
============================================================
Interactive fiscal-data / debt simulation environment.

Type 'help' for commands.
Type 'all' to run the complete pipeline.
Type 'quit' to exit.
============================================================
""")

    while True:
        try:
            command = input("\nCYTHANX > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting CYTHANX.")
            return 0

        if not command:
            continue

        if command in {"quit", "exit", "q"}:
            print("Exiting CYTHANX.")
            return 0

        if command in {"help", "?"}:
            print_help()
            continue

        if command == "clear":
            print("\033[2J\033[H", end="")
            continue

        action = COMMANDS.get(command)
        if action is None:
            print(f"Unknown command: {command!r}. Type 'help'.")
            continue

        try:
            action()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"Error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
