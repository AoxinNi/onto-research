# Data sources

Every place the workbench gets data from outside itself, as of branch `feature/local-first` (2026-11-15). The crypto
market and on-chain sources (C1–C4) were removed in commit 87c8956 and restored on 2026-11-15. Written as
the starting inventory for moving the data-source part into its own application.

There are two families, which today share almost no code:

- **Data department** (market projects): time series and news that a background loop collects on a schedule.
  Registered in `ontology/providers.py` and `ontology/sources.py`, collected by `ontology/collector.py`, and managed
  on the 数据源 page (`/api/sources…` in `workbench/server.py`).
- **Library** (research projects): papers, books and web pages, fetched once on request, with their metadata and
  files. Lives in `ontology/resolve.py`, `ontology/library.py`, `ontology/bibio.py` and `ontology/localfiles.py`,
  served by `workbench/library_api.py`. MinerU is registered as a provider, but it is used only by the library.

## Overview

| # | Source | Family | Kind | Key needed | Module |
|---|---|---|---|---|---|
| C1 | Binance | Data | Crypto prices, funding, open interest, polled | No | `ontology/sources.py` |
| C2 | mempool.space | Data | BTC hashrate and wallet balances, polled | No | `ontology/sources.py`, `ontology/onchain.py` |
| C3 | DefiLlama | Data | Stablecoin supply, polled | No | `ontology/sources.py` |
| C4 | Ethereum public RPC and TronGrid | Data | USDT/USDC freezes, polled | No (`ETH_RPC_URL`, `TRON_API_KEY` optional) | `ontology/onchain.py` |
| 1 | FRED (St. Louis Fed) | Data | Time series, polled | No | `ontology/sources.py` |
| 2 | 同花顺 iFinD (fuyao.aicubes.cn) | Data | Time series, polled, plus lookups | `THS_API_KEY` | `ontology/ths.py` |
| 3 | News RSS feeds (6 built in) | Data | Articles, polled | No | `ontology/news.py` |
| 4 | X, through Grok `x_search` | Data | Posts, on request | Model gateway (magpie) | `ontology/xsearch.py` |
| 5 | Custom sources: RSS / JSON / CSV / MCP | Data | User-defined | Depends on the source | `ontology/custom_sources.py` |
| 6 | MCP servers the user connects | Data | Tools; also feed type 5 | Headers set by the user | `ontology/mcp_clients.py` |
| 7 | Manual CSV import | Data | Time series, never polled | No | `ontology/sources.py` (`Manual`) |
| 8 | Derived news signals (heat, sentiment) | Data | Computed in house | No | `ontology/news.py` (`NewsSignal`) |
| 9 | DOI resolver (doi.org, with Crossref / DataCite behind it) | Library | Metadata | No | `ontology/resolve.py` |
| 10 | arXiv | Library | Metadata and PDF | No | `ontology/resolve.py` |
| 11 | OpenAlex | Library | Metadata and scholarly search | No | `ontology/resolve.py` |
| 12 | Open Library | Library | Book metadata (ISBN) | No | `ontology/resolve.py` |
| 13 | Any web page | Library | HTML or PDF capture | No (local workbench only) | `ontology/resolve.py` (`web`) |
| 14 | Reference-manager files (BibTeX, RIS, CSL-JSON) | Library | Import of entries | No | `ontology/bibio.py` |
| 15 | Uploaded files (PDF, TXT, Markdown) | Library | Files and extracted text | No | `ontology/library.py` |
| 16 | Local folders opened to the agent | Library | Files from disk | No | `ontology/localfiles.py` |
| 17 | MinerU | Library | PDF → Markdown and figures | Key ID and secret, or self-hosted | `ontology/mineru.py` |

## Data department

### C1. Binance (加密市场)

- **What it is:** spot candles, perpetual funding rates and open interest. Public endpoints, no key.
- **Sources:**

  | Name | Param | Endpoint |
  |---|---|---|
  | `binance_klines` | `symbol` (e.g. `BTCUSDT`), `interval` (`1h` / `4h` / `1d`) | `/api/v3/klines` on `api.binance.com`, `api1–3.binance.com` or `data-api.binance.vision` |
  | `binance_funding` | `symbol` | `https://fapi.binance.com/fapi/v1/fundingRate` |
  | `binance_oi` | `symbol`, `period` | `https://fapi.binance.com/futures/data/openInterestHist` |

- **Mirrors:** some regions get HTTP 451 from `api*.binance.com`. Each mirror gets one quick try, and the one that
  answered last time is tried first.
- **Live bar:** the candle still forming is kept as a provisional point (`payload.open`) and re-read until it
  closes.
- **Rate limit:** 0.1 s between requests.

### C2. mempool.space (加密市场)

- **`mempool_hashrate`:** daily average Bitcoin network hashrate in EH/s, from `/api/v1/mining/hashrate/{window}`.
- **`btc_wallet_balance`** (in `ontology/onchain.py`): a BTC address's balance, from `/api/address/{address}`.
- **Rate limit:** 0.5 s between requests.

### C3. DefiLlama (加密市场)

- **`defillama_stablecoin`:** circulating supply of a stablecoin (`id`: 1 = USDT, 2 = USDC), from
  `https://stablecoins.llama.fi/stablecoincharts/all`.
- **Rate limit:** 0.5 s between requests.

### C4. Ethereum and TronGrid (链上数据)

- **`stablecoin_freezes`:** USD frozen per hour by Tether and Circle blacklistings, often the first sign of a hack
  or a sanctions action. The amount frozen is the address's token balance.
  - Ethereum: USDT `AddedBlackList` and USDC `Blacklisted` events, via `eth_getLogs` on a public RPC
    (`ethereum-rpc.publicnode.com`, `rpc.mevblocker.io`). `ETH_RPC_URL` points it at your own node. Free nodes keep
    only about 30 hours of logs, which is enough for polling.
  - Tron: USDT `AddedBlackList`, via the TronGrid event API (`api.trongrid.io`). `TRON_API_KEY` is optional and
    raises the rate limit.
- **Freeze events:** when `COLLECT_ONCHAIN=1`, a single freeze above `FREEZE_EVENT_USD` becomes a pending Event node
  for review (`onchain.promote_freezes` in the collector pass).
- **Health check:** the 数据源 page has no live test for these two; its test button reports that no external
  service needs testing.

### 1. FRED

- **What it is:** US macro series: rates, the dollar index, M2, CPI, unemployment, the Fed balance sheet.
- **Endpoint:** the public CSV, `https://fred.stlouisfed.org/graph/fredgraph.csv?id=<series>&cosd=<date>`. No key; the
  documented API is not used.
- **Source:**
  - name `fred`, param `series` (for example `DFF`, `DGS10`, `M2SL`, `CPIAUCSL`, `DTWEXBGS`);
  - frequencies are mapped in `FRED_FREQ`, otherwise daily;
  - polled every 6 h, 10 years of backfill the first time, normal lag 14 days.
- **Limits:**
  - 0.5 s between requests;
  - FRED sometimes answers 200 with an HTML page, which is rejected;
  - the User-Agent `onto-research/0.1` is needed, because the CDN stalls on bare or browser-like agents.

### 2. 同花顺 iFinD (fuyao.aicubes.cn)

- **What it is:** A-share and index prices, valuations, financial indicators, ticker search, index constituents.
- **Auth:** `THS_API_KEY` in `.env`, sent as the `X-api-key` header. `THS_BASE_URL` overrides the host.
- **Response format:**
  - every answer comes in the envelope `{code, message, data}`;
  - `4001` or HTTP 429 means slow down (one retry after 2 s);
  - `3002` or `5003` means a report is not published yet;
  - times are milliseconds in Asia/Shanghai.
- **Rate limit:** 0.25 s between requests.
- **Series sources** (polled by the collector):

  | Name | Param | Endpoint |
  |---|---|---|
  | `ths_stock_daily` | `thscode`, `adjust` | `/api/a-share/prices/historical`, with `/api/a-share/prices/snapshot` for today's forming bar |
  | `ths_index_daily` | `thscode` | `/api/a-share-index/prices/historical`, `/api/a-share-index/prices/snapshot` |
  | `ths_valuation` | `thscode`, `metric` (`pe_ttm`, `pe_mrq`, `pb_mrq`, `ps_ttm`, `pcf_ttm`) | `/api/a-share/valuations/snapshot` |
  | `ths_fin_indicator` | `thscode`, `metric` (13 indicators in `INDICATORS`) | `/api/a-share/financials/indicators` |

- **Lookups** (the agent's `a_share_*` tools in `ontology/mcp_server.py`):

  | Tool | Endpoint |
  |---|---|
  | `a_share_search` | `/api/meta/tickers/search` |
  | `a_share_quote` | the price and valuation snapshot endpoints |
  | `a_share_financials` | `/api/a-share/financials/indicators` |
  | `a_share_index_members` | `/api/a-share-index/constituents/ths-stock-list` |

### 3. News RSS feeds

- **Built-in feeds** (`FEEDS` in `ontology/news.py`):

  | Id | URL | Language |
  |---|---|---|
  | `coindesk` | `https://www.coindesk.com/arc/outboundfeeds/rss/` | en |
  | `cointelegraph` | `https://cointelegraph.com/rss` | en |
  | `decrypt` | `https://decrypt.co/feed` | en |
  | `theblock` | `https://www.theblock.co/rss.xml` | en |
  | `bitcoinmagazine` | `https://bitcoinmagazine.com/feed` | en |
  | `panews` | `https://www.panewslab.com/rss.xml?lang=zh` | zh |

- **Pipeline** (one pass runs in the collector loop):
  1. Feeds are fetched every 15 minutes into `documents`, deduplicated by URL.
  2. A keyword screen against node names drops irrelevant articles.
  3. A model maps the remaining articles to nodes.
  4. Heat and sentiment series are computed (source 8).
  5. Repeated events are promoted into a changeset for review.
- **Switches:**
  - off unless `COLLECT_NEWS=1`;
  - custom RSS sources (type 5) are added to the built-in list (`CUSTOM_FEEDS`).

### 4. X, through Grok

- **What it is:** a search of recent posts on X.
- **How it is called:** a Responses API request (`/v1/responses`) with the `x_search` tool, sent to the model
  gateway at `ANTHROPIC_BASE_URL` (magpie) with model `X_SEARCH_MODEL` (default `grok/grok-4.7`).
- **Check on results:** every post's status id is decoded to its creation time, and a post that falls outside the
  search window is dropped. If the model did not run the tool, no posts are reported.
- **Where results go:** they are stored as documents in feed `x`, so they count toward news heat.
- **Called by:** on request only, through the agent tool `search_x`; it is not polled.

### 5. Custom sources (数据源 page)

- **What they are:** sources the user defines without code. They are stored in the table
  `onto_research.custom_sources`, shared by all projects on the workbench, and mirrored into `sources.SOURCES` and
  `news.CUSTOM_FEEDS` by `sync()`.
- **Kinds:**
  - `rss`: a news feed joined to the news desk.
  - `json`: an HTTP endpoint. `path` leads to the list of points; `time` and `value` name the fields.
  - `csv`: an HTTP endpoint with a header row; `time` and `value` name the columns.
  - `mcp`: a tool on a connected MCP server (type 6), called with fixed arguments; its answer is read like `json`.
- **Options:**
  - `{param}` placeholders in the URL or the MCP arguments become binding params, so one source serves many nodes;
  - with no `time` field, each fetch records one point stamped with the fetch time ("price now" turned into a series);
  - time formats `auto`, `iso`, `unix_s`, `unix_ms`;
  - at most 50,000 points.
- **Where available:** only on the local workbench and in the desktop app, because the server fetches arbitrary URLs.
- **Endpoints:** `POST /api/sources/custom`, `POST /api/sources/custom/test`, `PUT` / `DELETE /api/sources/custom/{name}`.

### 6. MCP servers

- **What they are:** Streamable HTTP MCP servers the user connects, each with a name, a URL and headers (usually
  API keys).
- **Storage:**
  - kept in `~/.config/onto-research/mcp.json` (or `WORKBENCH_LOCAL_MCP_SERVERS`), readable only by the user;
  - never stored in the database;
  - header values are shown masked.
- **Uses:**
  - the agent gets their tools;
  - custom sources of kind `mcp` call them.
- **Caching:** tool lists are cached for 5 minutes, an unreachable server is skipped for 1 minute, and a call times
  out after 120 s.
- **Where available:** not in a signed-in cloud workspace.

### 7. Manual CSV import

- **Source:** `manual`, with params `name` and `frequency`. Points are added by a person with
  `python -m ontology.cli import-csv`, for example ETF flows copied from Farside.
- **Polling:** never.

### 8. Derived news signals

- **Sources:** `news_heat` and `news_sentiment`, with param `node`. They are computed from news mentions and bound
  to the node automatically.
- **Polling:** never, because nothing is fetched.

## Library

All library network requests go through `resolve._fetch`. Its rules:

- **Hosts:** only those in `WORKBENCH_LIBRARY_FETCH_HOSTS` (default: `doi.org`, `dx.doi.org`, `api.crossref.org`,
  `data.crossref.org`, `api.datacite.org`, `data.datacite.org`, `export.arxiv.org`, `arxiv.org`, `api.openalex.org`,
  `openlibrary.org`). Redirects are followed only within that list, at most 3 hops.
- **Size caps:** 2 MB for metadata, 10 MB for a page, 50 MB for a PDF.
- **Metadata:** comes from a resolver, an import file or a person, never from the agent.

### 9. DOI

- **Request:** `https://doi.org/<doi>` with `Accept: application/vnd.citationstyles.csl+json`. Content negotiation
  answers with CSL-JSON from Crossref or DataCite.
- **Cleanup:** large fields that are never cited (references, licenses, funders and so on) are dropped.

### 10. arXiv

- **Metadata:** `https://export.arxiv.org/api/query?id_list=<id>` (Atom).
- **PDF:** `https://arxiv.org/pdf/<id>`, downloaded and attached unless `with_file` is false.
- **Ids:** new-style (`2310.05029`) and old-style (`cs.CL/0101001`), each with an optional version.

### 11. OpenAlex

- **Lookup:** `https://api.openalex.org/works/<W…>` returns CSL and the abstract, which is rebuilt from the inverted
  index.
- **Search:** `https://api.openalex.org/works?search=…&per-page=…`. It backs the agent tool `search_scholarly` and
  `GET /api/scholarly`.

### 12. Open Library

- **Request:** `https://openlibrary.org/api/books?bibkeys=ISBN:<isbn>&format=json&jscmd=data`, for book metadata.
- **Check:** the ISBN checksum is validated first.

### 13. Web pages

- **What it does:** `resolve.web(url)` fetches any public address.
  - For HTML, it reads the citation `<meta>` tags and extracts the page text, stored as `text/x-web-extract`.
  - For a PDF, it keeps the file.
- **Where available:** only when `WORKBENCH_LIBRARY_FETCH_ANY=1`, which is the default locally and off on the cloud
  server.
- **Address checks:**
  - addresses that are not public are refused;
  - the fake-IP range `198.18.0.0/15` (Clash or Surge in TUN mode) is allowed locally (`WORKBENCH_LIBRARY_FAKE_IP`).

### 14. Reference-manager files

- **Formats:** BibTeX, RIS and CSL-JSON, auto-detected (`bibio.detect`) and parsed in plain Python, with no pandoc.
- **Limits:** 5 MB and 2,000 entries. An unreadable entry is skipped and reported.
- **Endpoint:** `POST /api/library/import`. The way back out is `GET /api/library/export.bib`.

### 15. Uploaded files

- **Types:** PDF, plain text and Markdown, uploaded through `POST /api/library/upload` or
  `POST /api/library/{item_id}/files`.
- **Text extraction:**
  - PDFs are extracted page by page, up to 500 pages and 60 s;
  - text is split into 1,500-character chunks for search and quote checks;
  - files are stored once by SHA-256.

### 16. Local folders

- **Which folders:** only those listed in `ONTOLOGY_LIBRARY_FOLDERS` (absolute paths). Every path is resolved,
  symlinks included, and must stay inside one of them.
- **Files:** `.pdf`, `.md`, `.markdown`, `.txt`; listing shows up to 500, and adding takes up to 20 per call.
- **Agent tools:** `list_local_files`, `add_local_files`. A DOI printed in a PDF is resolved for metadata.
- **Where available:** not in a signed-in cloud workspace.

### 17. MinerU

- **What it does:** converts a library PDF to Markdown, `structured_content.json` and figures. The 精读 reading
  (`ontology/digest.py`) and the layout view are built from that output.
- **API:** the V1 Tasks API.
  1. Create a task.
  2. PUT the PDF to the link it returns.
  3. Poll the task.
  4. Download the result ZIP.
- **Where it runs:**
  - by default on the hosted mineru.net, which needs a key ID and secret and receives the PDF;
  - or on a self-hosted `mineru-kit api-server`, which needs no key.
- **Configuration:** set in 平台设置 and saved in `mineru.settings_file()`; `MINERU_API_URL` and `MINERU_API_KEY` are
  the fallback.
- **Cache:** results are cached on disk by PDF SHA-256 under `ONTO_PAPER_CACHE`, so the same PDF in two projects is
  converted once.

## Not counted as data sources

- **Model providers** (Anthropic, or the magpie gateway): they power the agent, the news extraction and the 精读
  reading. X search (source 4) is the one data source that goes through them.
- **CSL citation styles** (APA, Chicago, IEEE, GB/T 7714): files bundled in the repository, not fetched.
- **Project files and Big Bang merges** (`project.onto.json`, `ontology/bigbang.py`): data moving between projects
  of this app.
- **Type templates** (`ontology/schema/bindings.yaml`): would bind series to node types automatically; the list is
  currently empty.

## Shared machinery to carry over

These pieces are what the data-source part already depends on.

- **Provider registry** (`ontology/providers.py`):
  - id, category, homepage, docs, the environment variable that holds the key, the minimum interval between
    requests, and a note;
  - `throttle()` enforces the interval per process;
  - `save_key()` and `load_env_file()` manage `.env`, and keys never go into the database.
- **Source interface** (`ontology/sources.py`):
  - `Source` with `params`, `poll`, `backfill`, `lag`, `frequency()`, `fetch(params, since) -> [Obs(ts, value,
    payload)]`;
  - `_get()` with retries and a User-Agent;
  - `series_key()` for the canonical key.
- **Collector** (`ontology/collector.py`):
  - one pass a minute per active project;
  - each series is fetched when it is due and re-reads its last 3 points, because bars get revised;
  - a failed series is retried within 2 minutes;
  - `refresh_now()` fetches everything in parallel (8 threads).
- **Series storage** (`ontology/series.py`):
  - the tables `series` and `observations`, upserted by `(series_id, ts)`;
  - per-series health (`status`, `last_error`, `last_fetch_at`);
  - staleness takes into account the frequency and the publication lag.
- **Health and keys:** the 数据源 page's health view and test buttons, through `GET /api/sources`,
  `POST /api/sources/{provider_id}/test` and `PUT /api/sources/{provider_id}/key`.

Where the code is tied to the rest of the workbench:

- series reach the graph through `bindings`, which are reviewed as part of the ontology through `OntologyStore`;
- news extraction writes mentions against graph nodes and promotes events into changesets;
- library items and files live in each project's schema, next to excerpts, notes and groundings.

An independent app would need an interface at each of these three points.
