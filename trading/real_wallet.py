"""
Real Solana wallet using solders + solana-py.

All operations are *opt-in*. Even if MODE=REAL is set, the wallet does
nothing dangerous unless `settings.enable_real_trading` is True AND the
operator has explicitly confirmed via the safety flow in main.py.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import List, Optional

import base58

from config import settings
from utils.helpers import decrypt_secret
from utils.logger import get_logger

log = get_logger(__name__)


def _try_import_solders():
    """Lazy import - allow paper mode to run without solders installed."""
    from solders.keypair import Keypair  # noqa: PLC0415
    from solders.pubkey import Pubkey    # noqa: PLC0415
    from solders.transaction import VersionedTransaction  # noqa: PLC0415
    return Keypair, Pubkey, VersionedTransaction


def _try_import_solana_client():
    from solana.rpc.async_api import AsyncClient  # noqa: PLC0415
    from solana.rpc.commitment import Confirmed   # noqa: PLC0415
    from solana.rpc.types import TxOpts           # noqa: PLC0415
    return AsyncClient, Confirmed, TxOpts


# ---------------------------------------------------------------------------
# Keypair loading
# ---------------------------------------------------------------------------

def _load_keypair_from_secret(secret_b58: str):
    """Decode a base58 secret key (Phantom export format) into a Keypair."""
    Keypair, _, _ = _try_import_solders()
    secret_bytes = base58.b58decode(secret_b58.strip())
    if len(secret_bytes) == 64:
        return Keypair.from_bytes(secret_bytes)
    if len(secret_bytes) == 32:
        return Keypair.from_seed(secret_bytes)
    raise ValueError(f"Unexpected secret key length: {len(secret_bytes)}")


def _resolve_secret_from_env() -> Optional[str]:
    """Decrypt if WALLET_PRIVATE_KEY_ENC + passphrase present, else use plain."""
    if settings.wallet_private_key_enc and settings.wallet_enc_passphrase:
        try:
            return decrypt_secret(
                settings.wallet_private_key_enc, settings.wallet_enc_passphrase
            )
        except ValueError as exc:
            log.error("Failed to decrypt WALLET_PRIVATE_KEY_ENC: %s", exc)
            return None
    return settings.wallet_private_key or None


# ---------------------------------------------------------------------------
# Wallet wrapper
# ---------------------------------------------------------------------------

@dataclass
class RealWallet:
    """Wraps a primary keypair + a list of rotation keypairs."""

    primary: object                 # Keypair
    rotations: List[object]         # List[Keypair]
    rpc_url: str

    @classmethod
    def from_env(cls) -> Optional["RealWallet"]:
        secret = _resolve_secret_from_env()
        if not secret:
            log.warning("No wallet secret configured (REAL mode disabled).")
            return None
        try:
            primary = _load_keypair_from_secret(secret)
        except Exception as exc:
            log.error("Cannot load primary wallet: %s", exc)
            return None

        rotations: list = []
        for extra in settings.extra_wallets:
            try:
                # If looks like our encrypted blob, decrypt with same passphrase.
                if extra.startswith("gAAAA") or len(extra) > 90:
                    plain = decrypt_secret(extra, settings.wallet_enc_passphrase)
                    rotations.append(_load_keypair_from_secret(plain))
                else:
                    rotations.append(_load_keypair_from_secret(extra))
            except Exception as exc:
                log.warning("Skipping invalid rotation key: %s", exc)

        return cls(primary=primary, rotations=rotations, rpc_url=settings.effective_rpc)

    @property
    def pubkey_str(self) -> str:
        return str(self.primary.pubkey())

    # -- RPC ----------------------------------------------------------------

    async def get_sol_balance(self) -> float:
        AsyncClient, *_ = _try_import_solana_client()
        async with AsyncClient(self.rpc_url) as client:
            resp = await client.get_balance(self.primary.pubkey())
            lamports = resp.value if hasattr(resp, "value") else 0
            return lamports / 1_000_000_000

    async def get_token_balance(self, mint: str) -> float:
        """Total balance of `mint` across all token accounts."""
        AsyncClient, _, _ = _try_import_solana_client()
        _, Pubkey, _ = _try_import_solders()
        async with AsyncClient(self.rpc_url) as client:
            resp = await client.get_token_accounts_by_owner_json_parsed(
                self.primary.pubkey(),
                {"mint": str(Pubkey.from_string(mint))},
            )
            total = 0.0
            for acc in resp.value:
                try:
                    info = acc.account.data.parsed["info"]
                    amount = info["tokenAmount"]["uiAmount"] or 0
                    total += float(amount)
                except (KeyError, TypeError):
                    continue
            return total

    async def send_signed_tx(self, signed_tx_b64: str) -> Optional[str]:
        """Submit a base64-encoded *signed* VersionedTransaction. Returns sig."""
        if not settings.enable_real_trading:
            log.error("send_signed_tx blocked: ENABLE_REAL_TRADING=false")
            return None

        AsyncClient, Confirmed, TxOpts = _try_import_solana_client()
        _, _, VersionedTransaction = _try_import_solders()
        tx_bytes = base64.b64decode(signed_tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)

        async with AsyncClient(self.rpc_url) as client:
            sig_resp = await client.send_raw_transaction(
                bytes(tx),
                opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed),
            )
            sig = str(sig_resp.value) if hasattr(sig_resp, "value") else None
            if not sig:
                log.error("Transaction send returned no signature.")
                return None
            log.info("Submitted tx %s", sig)
            try:
                await client.confirm_transaction(sig_resp.value, Confirmed)
            except Exception as exc:
                log.warning("Confirmation timed out for %s: %s", sig, exc)
            return sig

    def sign_versioned_tx(self, unsigned_b64: str) -> str:
        """Sign a base64 VersionedTransaction message from Jupiter; return base64."""
        _, _, VersionedTransaction = _try_import_solders()
        raw = base64.b64decode(unsigned_b64)
        tx = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [self.primary])
        return base64.b64encode(bytes(signed)).decode("ascii")

    # -- rotation ----------------------------------------------------------

    def rotate(self) -> None:
        """Swap primary with next rotation wallet (if any)."""
        if not self.rotations:
            return
        new_primary = self.rotations.pop(0)
        self.rotations.append(self.primary)
        self.primary = new_primary
        log.info("Rotated wallet -> %s", self.pubkey_str[:10])


# Lazy singleton
_wallet: Optional[RealWallet] = None
_init_lock = asyncio.Lock()


async def get_real_wallet() -> Optional[RealWallet]:
    global _wallet
    async with _init_lock:
        if _wallet is None:
            _wallet = RealWallet.from_env()
        return _wallet
