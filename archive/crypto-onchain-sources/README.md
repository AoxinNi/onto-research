# Crypto market and on-chain data sources (archived)

These connectors were removed from the data source catalog on `feature/local-first`. This folder keeps the files
exactly as they were at commit 4269efa, so they can be restored. Nothing here is imported, packaged or tested.

## What was removed

| Category | Provider | Sources |
|---|---|---|
| 加密市场 (crypto) | Binance | `binance_klines`, `binance_funding`, `binance_oi` |
| 加密市场 (crypto) | mempool.space | `mempool_hashrate`, and the BTC wallet balances in `onchain.py` |
| 加密市场 (crypto) | DefiLlama | `defillama_stablecoin` |
| 链上数据 (onchain) | Ethereum public nodes, TronGrid | `stablecoin_freezes` (USDT/USDC blacklistings), freeze promotion to Event nodes |

Also removed with them: the provider health checks on the 数据源 page (`server_health_checks.py.txt`), the
freeze pass in the collector (`COLLECT_ONCHAIN`), the binding templates that used these sources (in the test
fixture `tests/fixtures/bindings.yaml`; the shipped `ontology/schema/bindings.yaml` had none), and their tests.

Kept on purpose: the `onchain_alerts` table (migration `009_onchain` has already run in existing databases), the
crypto news RSS feeds (provider `rss`, category 新闻资讯), and the market type pack.

## Files

- `ontology/sources.py`, `ontology/providers.py`: the whole files as they were; the crypto classes, parsers and
  provider entries are the parts that were taken out.
- `ontology/onchain.py`: the on-chain module, whole.
- `tests/test_onchain.py`, `tests/fixtures/bindings.yaml`: their tests and template fixtures.
- `server_health_checks.py.txt`: the `checks` table from `_test_provider` in `workbench/server.py`.

## Restoring

Copy `ontology/onchain.py` and `tests/test_onchain.py` back, restore the removed entries in `sources.py` and
`providers.py`, re-register `onchain.SOURCES` in `sources._register_providers`, and bring back the collector's
freeze pass and the health checks. Or take the files straight from git: `git show 4269efa:ontology/sources.py`.
