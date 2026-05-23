"""
Central configuration loader.

All modules import `settings` from here. We use pydantic for validation
and `python-dotenv` so a local `.env` file is auto-loaded.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Literal, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

ROOT_DIR: Path = Path(__file__).resolve().parent
ENV_FILE: Path = ROOT_DIR / ".env"

# Load .env if present. Silent if missing.
load_dotenv(ENV_FILE)

Mode = Literal["PAPER", "REAL"]


def _get(name: str, default: Optional[str] = None) -> str:
    val = os.getenv(name, default)
    return val if val is not None else ""


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseModel):
    """Validated runtime settings."""

    # Mode
    mode: Mode = Field(default="PAPER")
    enable_real_trading: bool = Field(default=False)

    # Multi-chain
    enabled_chains: List[str] = Field(default_factory=lambda: ["solana"])

    # RPC (Solana)
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_ws_url: str = "wss://api.mainnet-beta.solana.com"
    helius_api_key: str = ""
    quicknode_rpc_url: str = ""

    # RPC (EVM) - per-chain primary URLs
    ethereum_rpc_url: str = ""
    bsc_rpc_url: str = ""
    polygon_rpc_url: str = ""
    base_rpc_url: str = ""
    arbitrum_rpc_url: str = ""
    optimism_rpc_url: str = ""
    avalanche_rpc_url: str = ""

    # RPC fallback CSVs
    ethereum_rpc_fallbacks: List[str] = Field(default_factory=list)
    bsc_rpc_fallbacks: List[str] = Field(default_factory=list)
    polygon_rpc_fallbacks: List[str] = Field(default_factory=list)
    base_rpc_fallbacks: List[str] = Field(default_factory=list)
    arbitrum_rpc_fallbacks: List[str] = Field(default_factory=list)
    optimism_rpc_fallbacks: List[str] = Field(default_factory=list)
    avalanche_rpc_fallbacks: List[str] = Field(default_factory=list)

    # Wallets
    wallet_private_key: str = ""
    wallet_private_key_enc: str = ""
    wallet_enc_passphrase: str = ""
    extra_wallets: List[str] = Field(default_factory=list)

    # Paper wallet
    paper_starting_balance_usdt: float = 100.0

    # Trading params
    default_buy_amount_sol: float = 0.05
    max_open_positions: int = 5
    max_daily_loss_usdt: float = 20.0
    cooldown_after_loss_secs: int = 120
    slippage_bps: int = 300
    priority_fee_microlamports: int = 200_000
    take_profit: float = 0.50
    stop_loss: float = 0.20
    trailing_stop: float = 0.15

    # AI
    ai_buy_threshold: float = 0.65
    ai_sell_threshold: float = 0.35

    # Scalp mode (aggressive fast-exit)
    # When scalp_mode=true:
    #   - regular TP / SL / trailing / chart-exits are bypassed
    #   - exit fires on (profit_pct >= scalp_profit_pct) OR (profit_usd >= scalp_profit_usd)
    #   - safety: if scalp_max_loss_pct hits, still exit (so account can't blow up)
    #
    # IMPORTANT MATH:  with 50% winrate, you need (TP / SL) > 1 to break even.
    # Default ratio here = 3% TP / 8% SL = 0.375. That requires ~73% winrate
    # just to break even. Combined with the new entry-quality gates this is
    # achievable; in pure-luck mode (no filters) you will bleed.
    scalp_mode: bool = False
    scalp_profit_pct: float = 0.03         # 3% gross (~2% net after slippage)
    scalp_profit_usd: float = 1.0
    scalp_max_loss_pct: float = 0.08       # -8% hard stop (was -20%)

    # EXIT-ON-PROFIT mode (most aggressive).
    # When exit_on_profit=true, *every* other exit rule is bypassed:
    #   no take profit %, no stop loss %, no trailing, no scalp targets,
    #   no chart-AI exits, no orderflow exits.
    # Only TWO triggers remain:
    #   1. PnL_usd >= exit_on_profit_min_usd  -> SELL (any positive profit)
    #   2. loss_pct >= emergency_max_loss_pct -> SELL (account safety net)
    # The emergency stop is INTENTIONALLY non-disableable - without it a
    # rugged token sits at -99% forever and your bankroll dies.
    exit_on_profit: bool = False
    exit_on_profit_min_usd: float = 0.10    # min profit (USD) to trigger. 0 = any +ve.
    emergency_max_loss_pct: float = 0.50    # -50% absolute floor

    # DEX bases
    dexscreener_base: str = "https://api.dexscreener.com"
    raydium_base: str = "https://api.raydium.io"
    pumpfun_base: str = "https://frontend-api.pump.fun"
    jupiter_base: str = "https://quote-api.jup.ag/v6"

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = False

    # Logging
    log_level: str = "INFO"
    log_file: str = "logs/bot.log"

    # Paths
    # `data_dir` is the persistent-storage root. On Railway / Docker mount a
    # volume at /data and set DATA_DIR=/data so paper_wallet.json, the
    # SQLite database, and trained AI models survive restarts.
    data_dir: str = str(ROOT_DIR)
    db_path: str = str(ROOT_DIR / "database.db")
    models_dir: str = str(ROOT_DIR / "ai" / "saved_models")

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, v: str) -> str:
        if not v:
            return "PAPER"
        v = str(v).strip().upper()
        return "REAL" if v == "REAL" else "PAPER"

    @property
    def is_real(self) -> bool:
        return self.mode == "REAL" and self.enable_real_trading

    @property
    def effective_rpc(self) -> str:
        if self.helius_api_key:
            return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        if self.quicknode_rpc_url:
            return self.quicknode_rpc_url
        return self.solana_rpc_url

    def chain_rpcs(self, chain: str) -> List[str]:
        """Return [primary, ...fallbacks] for a chain.

        Drops empties and dedups. Crucially, when a premium endpoint
        (Helius / QuickNode) is configured, the rate-limited public
        mainnet URL is NOT added as a fallback - it just spams 429s
        every health-probe cycle.
        """
        chain = chain.lower()
        if chain == "solana":
            urls = [self.effective_rpc]
            # Only add the public RPC if no premium endpoint is set.
            if not (self.helius_api_key or self.quicknode_rpc_url):
                urls.append(self.solana_rpc_url)
        else:
            primary = getattr(self, f"{chain}_rpc_url", "") or ""
            fallbacks = getattr(self, f"{chain}_rpc_fallbacks", []) or []
            urls = [primary, *fallbacks]
        # Dedup while preserving order.
        seen: set[str] = set()
        out: List[str] = []
        for u in urls:
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out


def load_settings() -> Settings:
    # Resolve persistent-data dir (volume mount on Railway / Docker).
    data_dir = _get("DATA_DIR", str(ROOT_DIR)) or str(ROOT_DIR)
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    s = Settings(
        mode=_get("MODE", "PAPER"),
        enable_real_trading=_get_bool("ENABLE_REAL_TRADING", False),
        enabled_chains=_split_csv(_get("ENABLED_CHAINS", "solana")),
        solana_rpc_url=_get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
        solana_ws_url=_get("SOLANA_WS_URL", "wss://api.mainnet-beta.solana.com"),
        helius_api_key=_get("HELIUS_API_KEY", ""),
        quicknode_rpc_url=_get("QUICKNODE_RPC_URL", ""),
        ethereum_rpc_url=_get("ETHEREUM_RPC_URL", ""),
        bsc_rpc_url=_get("BSC_RPC_URL", ""),
        polygon_rpc_url=_get("POLYGON_RPC_URL", ""),
        base_rpc_url=_get("BASE_RPC_URL", ""),
        arbitrum_rpc_url=_get("ARBITRUM_RPC_URL", ""),
        optimism_rpc_url=_get("OPTIMISM_RPC_URL", ""),
        avalanche_rpc_url=_get("AVALANCHE_RPC_URL", ""),
        ethereum_rpc_fallbacks=_split_csv(_get("ETHEREUM_RPC_FALLBACKS", "")),
        bsc_rpc_fallbacks=_split_csv(_get("BSC_RPC_FALLBACKS", "")),
        polygon_rpc_fallbacks=_split_csv(_get("POLYGON_RPC_FALLBACKS", "")),
        base_rpc_fallbacks=_split_csv(_get("BASE_RPC_FALLBACKS", "")),
        arbitrum_rpc_fallbacks=_split_csv(_get("ARBITRUM_RPC_FALLBACKS", "")),
        optimism_rpc_fallbacks=_split_csv(_get("OPTIMISM_RPC_FALLBACKS", "")),
        avalanche_rpc_fallbacks=_split_csv(_get("AVALANCHE_RPC_FALLBACKS", "")),
        wallet_private_key=_get("WALLET_PRIVATE_KEY", ""),
        wallet_private_key_enc=_get("WALLET_PRIVATE_KEY_ENC", ""),
        wallet_enc_passphrase=_get("WALLET_ENC_PASSPHRASE", ""),
        extra_wallets=_split_csv(_get("EXTRA_WALLETS", "")),
        paper_starting_balance_usdt=_get_float("PAPER_STARTING_BALANCE_USDT", 100.0),
        default_buy_amount_sol=_get_float("DEFAULT_BUY_AMOUNT_SOL", 0.05),
        max_open_positions=_get_int("MAX_OPEN_POSITIONS", 5),
        max_daily_loss_usdt=_get_float("MAX_DAILY_LOSS_USDT", 20.0),
        cooldown_after_loss_secs=_get_int("COOLDOWN_AFTER_LOSS_SECS", 120),
        slippage_bps=_get_int("SLIPPAGE_BPS", 300),
        priority_fee_microlamports=_get_int("PRIORITY_FEE_MICROLAMPORTS", 200_000),
        take_profit=_get_float("TAKE_PROFIT", 0.50),
        stop_loss=_get_float("STOP_LOSS", 0.20),
        trailing_stop=_get_float("TRAILING_STOP", 0.15),
        ai_buy_threshold=_get_float("AI_BUY_THRESHOLD", 0.65),
        ai_sell_threshold=_get_float("AI_SELL_THRESHOLD", 0.35),
        scalp_mode=_get_bool("SCALP_MODE", False),
        scalp_profit_pct=_get_float("SCALP_PROFIT_PCT", 0.03),
        scalp_profit_usd=_get_float("SCALP_PROFIT_USD", 1.0),
        scalp_max_loss_pct=_get_float("SCALP_MAX_LOSS_PCT", 0.08),
        exit_on_profit=_get_bool("EXIT_ON_PROFIT", False),
        exit_on_profit_min_usd=_get_float("EXIT_ON_PROFIT_MIN_USD", 0.10),
        emergency_max_loss_pct=_get_float("EMERGENCY_MAX_LOSS_PCT", 0.50),
        dexscreener_base=_get("DEXSCREENER_BASE", "https://api.dexscreener.com"),
        raydium_base=_get("RAYDIUM_BASE", "https://api.raydium.io"),
        pumpfun_base=_get("PUMPFUN_BASE", "https://frontend-api.pump.fun"),
        jupiter_base=_get("JUPITER_BASE", "https://quote-api.jup.ag/v6"),
        telegram_bot_token=_get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=_get("TELEGRAM_CHAT_ID", ""),
        telegram_enabled=_get_bool("TELEGRAM_ENABLED", False),
        log_level=_get("LOG_LEVEL", "INFO"),
        log_file=_get("LOG_FILE", str(Path(data_dir) / "logs" / "bot.log")),
        data_dir=data_dir,
        db_path=_get("DB_PATH", str(Path(data_dir) / "database.db")),
        models_dir=_get("MODELS_DIR", str(Path(data_dir) / "saved_models")),
    )
    Path(s.models_dir).mkdir(parents=True, exist_ok=True)
    Path(s.log_file).parent.mkdir(parents=True, exist_ok=True)
    return s


settings: Settings = load_settings()
