"""
Uniswap V2-compatible swap client.

One module covers:
  - Uniswap V2 (Ethereum)
  - PancakeSwap V2 (BSC)
  - QuickSwap (Polygon)
  - SushiSwap V2 (anywhere)
  - TraderJoe (Avalanche)
  - BaseSwap V2 (Base) / Uniswap V2 on L2s

Every one of them is byte-compatible with the V2 router ABI; only the
addresses change. Per-chain addresses are in chains/EVM_CHAINS.

REAL-MODE OPERATIONS:
  - swap_native_for_token (BUY)
  - swap_token_for_native (SELL)
  - get_amounts_out       (quote, no signing)
"""
from __future__ import annotations

import time
from typing import List, Optional

from chains import EVM_CHAINS, Chain
from chains.evm.rpc import get_w3
from chains.evm.wallet import ERC20_ABI, get_evm_wallet
from config import settings
from utils.logger import get_logger

log = get_logger(__name__)

ROUTER_V2_ABI = [
    {
        "name": "getAmountsOut",
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "path", "type": "address[]"},
        ],
        "stateMutability": "view", "type": "function",
    },
    {
        "name": "swapExactETHForTokensSupportingFeeOnTransferTokens",
        "outputs": [],
        "inputs": [
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "stateMutability": "payable", "type": "function",
    },
    {
        "name": "swapExactTokensForETHSupportingFeeOnTransferTokens",
        "outputs": [],
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "stateMutability": "nonpayable", "type": "function",
    },
]


class UniswapV2:
    """Stateless helper - methods take `chain` (the Chain enum value)."""

    def _spec(self, chain: Chain):
        spec = EVM_CHAINS.get(chain)
        if spec is None:
            raise ValueError(f"no EVM spec for chain {chain}")
        return spec

    def _router(self, chain: Chain):
        spec = self._spec(chain)
        w3 = get_w3(chain.value)
        if w3 is None:
            return None
        return w3.eth.contract(
            address=w3.to_checksum_address(spec.uniswap_v2_router),
            abi=ROUTER_V2_ABI,
        )

    # -- quote -------------------------------------------------------------

    def quote(self, chain: Chain, amount_in_wei: int, token_out: str) -> Optional[int]:
        """How many `token_out` units do we get for `amount_in_wei` of native?"""
        router = self._router(chain)
        if router is None:
            return None
        spec = self._spec(chain)
        w3 = get_w3(chain.value)
        try:
            out = router.functions.getAmountsOut(
                amount_in_wei,
                [w3.to_checksum_address(spec.wrapped_native),
                 w3.to_checksum_address(token_out)],
            ).call()
            return int(out[-1])
        except Exception as exc:
            log.debug("quote failed (%s, %s): %s", chain.value, token_out[:10], exc)
            return None

    # -- swap --------------------------------------------------------------

    def swap_native_for_token(
        self,
        chain: Chain,
        token_out: str,
        amount_in_wei: int,
        slippage_bps: Optional[int] = None,
    ) -> Optional[str]:
        """BUY. Returns tx hash on success."""
        if not settings.enable_real_trading:
            log.error("EVM swap blocked: ENABLE_REAL_TRADING=false")
            return None
        wallet = get_evm_wallet()
        if wallet is None:
            log.error("EVM wallet missing")
            return None
        w3 = get_w3(chain.value)
        spec = self._spec(chain)
        router = self._router(chain)
        if w3 is None or router is None:
            return None

        slip = slippage_bps if slippage_bps is not None else settings.slippage_bps
        try:
            quote_out = self.quote(chain, amount_in_wei, token_out)
            if not quote_out:
                return None
            amount_out_min = int(quote_out * (10_000 - slip) / 10_000)
            deadline = int(time.time()) + 60

            tx = router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
                amount_out_min,
                [w3.to_checksum_address(spec.wrapped_native),
                 w3.to_checksum_address(token_out)],
                w3.to_checksum_address(wallet.address),
                deadline,
            ).build_transaction({
                "from": wallet.address,
                "value": amount_in_wei,
                "nonce": w3.eth.get_transaction_count(wallet.address, "pending"),
                "gasPrice": int(w3.eth.gas_price * 1.25),  # priority boost
                "chainId": spec.chain_id,
            })
            # Estimate gas with margin
            try:
                tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.30)
            except Exception:
                tx["gas"] = 350_000

            signed = w3.eth.account.sign_transaction(tx, wallet.private_key)
            h = w3.eth.send_raw_transaction(signed.raw_transaction)
            return h.hex()
        except Exception as exc:
            log.error("swap_native_for_token failed: %s", exc)
            return None

    def swap_token_for_native(
        self,
        chain: Chain,
        token_in: str,
        amount_in_units: int,
        slippage_bps: Optional[int] = None,
    ) -> Optional[str]:
        """SELL. Caller must ensure router has allowance (call `approve` first)."""
        if not settings.enable_real_trading:
            log.error("EVM swap blocked: ENABLE_REAL_TRADING=false")
            return None
        wallet = get_evm_wallet()
        if wallet is None:
            return None
        w3 = get_w3(chain.value)
        spec = self._spec(chain)
        router = self._router(chain)
        if w3 is None or router is None:
            return None

        slip = slippage_bps if slippage_bps is not None else settings.slippage_bps
        try:
            # Get quote for amount_out_min
            quote_router = router.functions.getAmountsOut(
                amount_in_units,
                [w3.to_checksum_address(token_in),
                 w3.to_checksum_address(spec.wrapped_native)],
            ).call()
            amount_out_min = int(quote_router[-1] * (10_000 - slip) / 10_000)
            deadline = int(time.time()) + 60

            tx = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
                amount_in_units,
                amount_out_min,
                [w3.to_checksum_address(token_in),
                 w3.to_checksum_address(spec.wrapped_native)],
                w3.to_checksum_address(wallet.address),
                deadline,
            ).build_transaction({
                "from": wallet.address,
                "nonce": w3.eth.get_transaction_count(wallet.address, "pending"),
                "gasPrice": int(w3.eth.gas_price * 1.25),
                "chainId": spec.chain_id,
            })
            try:
                tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.30)
            except Exception:
                tx["gas"] = 400_000

            signed = w3.eth.account.sign_transaction(tx, wallet.private_key)
            h = w3.eth.send_raw_transaction(signed.raw_transaction)
            return h.hex()
        except Exception as exc:
            log.error("swap_token_for_native failed: %s", exc)
            return None


uniswap_v2 = UniswapV2()
