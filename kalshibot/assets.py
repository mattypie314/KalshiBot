from __future__ import annotations

import re
from dataclasses import dataclass


SECTIONS = ("crypto", "commodities", "sports")

SECTION_LABELS = {
    "crypto": "Crypto",
    "commodities": "Commodities",
    "sports": "Sports Bets",
}

KALSHI_CATEGORY = {
    "crypto": "Crypto",
    "commodities": "Commodities",
    "sports": "Sports",
}


@dataclass(frozen=True)
class Asset:
    key: str
    display: str
    yahoo: str
    annual_vol: float
    coinbase: str | None = None
    tokens: tuple[str, ...] = ()


CRYPTO_ASSETS: tuple[Asset, ...] = (
    Asset("BTC", "Bitcoin", "BTC-USD", 0.62, "BTC-USD", ("BITCOIN", "BTC")),
    Asset("ETH", "Ethereum", "ETH-USD", 0.70, "ETH-USD", ("ETHEREUM", "ETH")),
    Asset("SOL", "Solana", "SOL-USD", 0.85, "SOL-USD", ("SOLANA", "SOL")),
    Asset("XRP", "XRP", "XRP-USD", 0.80, "XRP-USD", ("XRP",)),
    Asset("DOGE", "Dogecoin", "DOGE-USD", 0.95, "DOGE-USD", ("DOGECOIN", "DOGE")),
    Asset("SHIB", "Shiba Inu", "SHIB-USD", 1.10, "SHIB-USD", ("SHIBA", "SHIB")),
    Asset("BNB", "BNB", "BNB-USD", 0.70, "BNB-USD", ("BNB",)),
    Asset("ADA", "Cardano", "ADA-USD", 0.80, "ADA-USD", ("CARDANO", "ADA")),
    Asset("AVAX", "Avalanche", "AVAX-USD", 0.85, "AVAX-USD", ("AVALANCHE", "AVAX")),
    Asset("LINK", "Chainlink", "LINK-USD", 0.80, "LINK-USD", ("CHAINLINK", "LINK")),
    Asset("LTC", "Litecoin", "LTC-USD", 0.70, "LTC-USD", ("LITECOIN", "LTC")),
    Asset("PEPE", "Pepe", "PEPE-USD", 1.20, "PEPE-USD", ("PEPE",)),
    Asset("SUI", "Sui", "SUI-USD", 0.90, "SUI-USD", ("SUI",)),
)

COMMODITY_ASSETS: tuple[Asset, ...] = (
    Asset("GOLD", "Gold", "GC=F", 0.16, tokens=("GOLD", "XAU")),
    Asset("SILVER", "Silver", "SI=F", 0.28, tokens=("SILVER", "XAG")),
    Asset("COPPER", "Copper", "HG=F", 0.24, tokens=("COPPER",)),
    Asset("WTI", "WTI crude", "CL=F", 0.36, tokens=("WTI",)),
    Asset("BRENT", "Brent crude", "BZ=F", 0.34, tokens=("BRENT",)),
    Asset("NATGAS", "Natural gas", "NG=F", 0.55, tokens=("NATURAL GAS", "NATGAS", "NATURALGAS")),
    Asset("RBOB", "RBOB gasoline", "RB=F", 0.38, tokens=("RBOB", "GASOLINE")),
)

_ALL_ASSETS = CRYPTO_ASSETS + COMMODITY_ASSETS
ASSETS_BY_KEY = {asset.key: asset for asset in _ALL_ASSETS}


def asset_by_key(key: str | None) -> Asset | None:
    if not key:
        return None
    return ASSETS_BY_KEY.get(str(key).upper())


def identify_asset(series_ticker: str, title: str, subtitle: str = "") -> Asset | None:
    ticker = series_ticker.upper().replace("-", "")
    blob = f"{title} {subtitle}".upper()
    ranked = sorted(_ALL_ASSETS, key=lambda a: max(len(t) for t in a.tokens), reverse=True)
    for asset in ranked:
        tokens = sorted(asset.tokens, key=len, reverse=True)
        for token in tokens:
            compact = token.replace(" ", "")
            if ticker.startswith(f"KX{compact}") or ticker.startswith(compact):
                return asset
            if re.search(rf"\b{re.escape(token)}\b", blob):
                return asset
    return None
