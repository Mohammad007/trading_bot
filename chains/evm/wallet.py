"""
EVM wallet wrapper.

Holds an eth_account `LocalAccount` per chain. Reads native + ERC-20
balances, signs transactions, and sends them through web3.py.

Keys are loaded from env exactly like the Solana wallet:
  EVM_PRIVATE_KEY            - hex (0x...) plaintext (DEV ONLY)
  EVM_PRIVATE_KEY_ENC        - encrypted blob from utils.helpers.encrypt_secret
  WALLET_ENC_PASSPHRASE      - reused
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from chains.evm.rpc import get_w3
from config import settings
from utils.helpers import decrypt_secret
from utils.logger import get_logger

log = get_logger(__name__)

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [],
     "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [],
     "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": False, "inputs": [
        {"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [
        {"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]


def _resolve_key() -> Optional[str]:
    enc = os.getenv("EVM_PRIVATE_KEY_ENC", "")
    pp = settings.wallet_enc_passphrase
    if enc and pp:
        try:
            return decrypt_secret(enc, pp)
        except ValueError as exc:
            log.error("Failed to decrypt EVM_PRIVATE_KEY_ENC: %s", exc)
            return None
    return os.getenv("EVM_PRIVATE_KEY", "") or None


@dataclass
class EVMWallet:
    address: str
    private_key: str

    @classmethod
    def from_env(cls) -> Optional["EVMWallet"]:
        key = _resolve_key()
        if not key:
            return None
        try:
            from eth_account import Account  # noqa: PLC0415
            acct = Account.from_key(key)
            return cls(address=acct.address, private_key=key)
        except Exception as exc:
            log.error("EVM wallet load failed: %s", exc)
            return None

    # -- balances -----------------------------------------------------------

    def native_balance(self, chain: str) -> float:
        w3 = get_w3(chain)
        if w3 is None:
            return 0.0
        try:
            bal = w3.eth.get_balance(self.address)
            return float(w3.from_wei(bal, "ether"))
        except Exception as exc:
            log.debug("native_balance(%s) failed: %s", chain, exc)
            return 0.0

    def token_balance(self, chain: str, token_address: str) -> float:
        w3 = get_w3(chain)
        if w3 is None:
            return 0.0
        try:
            contract = w3.eth.contract(
                address=w3.to_checksum_address(token_address), abi=ERC20_ABI,
            )
            raw = contract.functions.balanceOf(self.address).call()
            decimals = contract.functions.decimals().call()
            return float(raw) / (10 ** int(decimals))
        except Exception as exc:
            log.debug("token_balance(%s, %s) failed: %s", chain, token_address[:10], exc)
            return 0.0

    # -- approve / send ------------------------------------------------------

    def approve(self, chain: str, token_address: str, spender: str, amount: int) -> Optional[str]:
        if not settings.enable_real_trading:
            log.error("approve blocked: ENABLE_REAL_TRADING=false")
            return None
        w3 = get_w3(chain)
        if w3 is None:
            return None
        try:
            contract = w3.eth.contract(
                address=w3.to_checksum_address(token_address), abi=ERC20_ABI,
            )
            tx = contract.functions.approve(
                w3.to_checksum_address(spender), amount,
            ).build_transaction({
                "from": self.address,
                "nonce": w3.eth.get_transaction_count(self.address, "pending"),
                "gasPrice": w3.eth.gas_price,
                "chainId": w3.eth.chain_id,
            })
            signed = w3.eth.account.sign_transaction(tx, self.private_key)
            h = w3.eth.send_raw_transaction(signed.raw_transaction)
            return h.hex()
        except Exception as exc:
            log.error("approve failed: %s", exc)
            return None


# Lazy singleton
_wallet: Optional[EVMWallet] = None


def get_evm_wallet() -> Optional[EVMWallet]:
    global _wallet
    if _wallet is None:
        _wallet = EVMWallet.from_env()
    return _wallet
