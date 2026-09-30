"""Cross-venue symbol handling. Only crypto passes: stocks, indices, commodities and FX are dropped."""

NON_CRYPTO = {
    "GOLD", "SILVER", "XAU", "XAG", "XAUT", "PAXG", "BRENTOIL", "WTIOIL", "NATGAS", "COPPER",
    "SPY", "QQQ", "SPX", "NDX", "SPCX", "EUR", "GBP", "JPY", "AUD", "CHF", "CAD",
}


def crypto_symbol(raw: str) -> str | None:
    """Normalize a venue's market symbol, or None if it isn't a crypto asset.

    - "xyz:TSLA": Hyperliquid builder-deployed markets are stocks/commodities → dropped
    - "XAUT.v2" → "XAUT" (then dropped as gold)
    - "APE_deprecated (deprecated)" → dropped
    """
    if ":" in raw or "deprecated" in raw.lower():
        return None
    symbol = raw.split(".")[0].strip().upper()
    if not symbol or symbol in NON_CRYPTO:
        return None
    return symbol
