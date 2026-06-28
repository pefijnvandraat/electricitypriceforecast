"""Convert wholesale price (EUR/MWh) to consumer prices (EUR/kWh)."""
import numpy as np


def wholesale_kwh(price_eur_mwh):
    return np.asarray(price_eur_mwh, dtype=float) / 1000.0


def all_in_kwh(price_eur_mwh, taxes):
    """All-in consumer price per kWh: (wholesale + markup + energy tax) * (1 + VAT).

    The annual fixed reduction (belastingvermindering) is NOT a per-kWh term and
    is carried in meta for the UI to apply on a yearly-cost basis.
    """
    kale = np.asarray(price_eur_mwh, dtype=float) / 1000.0
    markup = float(taxes.get("leveranciersopslag_eur_per_kwh", 0.0))
    energy_tax = float(taxes.get("energiebelasting_eur_per_kwh", 0.0))
    vat = float(taxes.get("btw_pct", 0.0)) / 100.0
    return (kale + markup + energy_tax) * (1.0 + vat)
