"""
Multi-chain abstraction.

Currently shipped:
  - solana   (via existing solders / Jupiter integration)
  - evm      (one module covering Ethereum, BSC, Polygon, Base,
              Arbitrum, Optimism, Avalanche, and any other EVM L1/L2)
  - tron     (basic - SunSwap routing)

NOT shipped: Sui, Aptos. Their Python SDKs are not production-stable
enough to ship as "production-grade". If you need them, drop a module
into chains/sui or chains/aptos following the EVM template.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Chain(str, Enum):
    SOLANA = "solana"
    ETHEREUM = "ethereum"
    BSC = "bsc"
    POLYGON = "polygon"
    BASE = "base"
    ARBITRUM = "arbitrum"
    OPTIMISM = "optimism"
    AVALANCHE = "avalanche"
    TRON = "tron"

    @classmethod
    def is_evm(cls, chain: "Chain") -> bool:
        return chain in {cls.ETHEREUM, cls.BSC, cls.POLYGON, cls.BASE,
                          cls.ARBITRUM, cls.OPTIMISM, cls.AVALANCHE}


@dataclass(frozen=True)
class EVMChainSpec:
    """Per-EVM-chain constants."""
    chain: Chain
    chain_id: int
    native_symbol: str
    wrapped_native: str       # WETH/WBNB/WMATIC etc.
    uniswap_v2_router: str    # the canonical V2-style router on this chain
    uniswap_v2_factory: str
    weth_pair_token: Optional[str] = None   # USDC/USDT canonical


EVM_CHAINS: dict[Chain, EVMChainSpec] = {
    Chain.ETHEREUM: EVMChainSpec(
        chain=Chain.ETHEREUM, chain_id=1,
        native_symbol="ETH",
        wrapped_native="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        uniswap_v2_router="0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        uniswap_v2_factory="0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        weth_pair_token="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
    ),
    Chain.BSC: EVMChainSpec(
        chain=Chain.BSC, chain_id=56,
        native_symbol="BNB",
        wrapped_native="0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        uniswap_v2_router="0x10ED43C718714eb63d5aA57B78B54704E256024E",   # PancakeSwap V2
        uniswap_v2_factory="0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73",
        weth_pair_token="0x55d398326f99059fF775485246999027B3197955",   # USDT(BSC)
    ),
    Chain.POLYGON: EVMChainSpec(
        chain=Chain.POLYGON, chain_id=137,
        native_symbol="MATIC",
        wrapped_native="0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
        uniswap_v2_router="0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff",   # QuickSwap
        uniswap_v2_factory="0x5757371414417b8C6CAad45bAeF941aBc7d3Ab32",
        weth_pair_token="0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    ),
    Chain.BASE: EVMChainSpec(
        chain=Chain.BASE, chain_id=8453,
        native_symbol="ETH",
        wrapped_native="0x4200000000000000000000000000000000000006",
        uniswap_v2_router="0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        uniswap_v2_factory="0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
        weth_pair_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",   # USDC
    ),
    Chain.ARBITRUM: EVMChainSpec(
        chain=Chain.ARBITRUM, chain_id=42161,
        native_symbol="ETH",
        wrapped_native="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        uniswap_v2_router="0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        uniswap_v2_factory="0x6EcCab422D763aC031210895C81787E87B43A652",
        weth_pair_token="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",   # USDC
    ),
    Chain.OPTIMISM: EVMChainSpec(
        chain=Chain.OPTIMISM, chain_id=10,
        native_symbol="ETH",
        wrapped_native="0x4200000000000000000000000000000000000006",
        uniswap_v2_router="0x4A7b5Da61326A6379179b40d00F57E5bbDC962c2",
        uniswap_v2_factory="0x0c3c1c532F1e39EdF36BE9Fe0bE1410313E074Bf",
        weth_pair_token="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    ),
    Chain.AVALANCHE: EVMChainSpec(
        chain=Chain.AVALANCHE, chain_id=43114,
        native_symbol="AVAX",
        wrapped_native="0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",
        uniswap_v2_router="0x60aE616a2155Ee3d9A68541Ba4544862310933d4",   # TraderJoe
        uniswap_v2_factory="0x9Ad6C38BE94206cA50bb0d90783181662f0Cfa10",
        weth_pair_token="0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
    ),
}
