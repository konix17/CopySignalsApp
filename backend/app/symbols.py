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


def normalize_coin(coin: str) -> tuple[str | None, float]:
    """Map a Hyperliquid coin to a cross-venue symbol and a price multiplier (used by the research scripts).

    "kPEPE" is quoted per 1000 PEPE, so its price is divided by 1000.
    Non-crypto markets (builder-deployed "xyz:TSLA" etc.) map to None.
    """
    if len(coin) > 1 and coin[0] == "k" and coin[1:].isupper():
        return crypto_symbol(coin[1:]), 1 / 1000
    return crypto_symbol(coin), 1.0
