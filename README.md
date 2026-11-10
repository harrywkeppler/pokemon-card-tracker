# Pokémon card tracker

Scans every Pokémon card (English and Japanese) once a day, keeps its own price
history, and flags cards priced **$100–$5,000** whose price has moved by an
unusual amount. Results go to a dashboard page and to `REPORT.md`.

It runs free on GitHub Actions. Nothing needs to stay switched on at home.

## What you get

- **Dashboard** (`docs/index.html`, served by GitHub Pages): today's alerts, top
  gainers and decliners, every tracked card with 1/7/30-day changes, a 60-day
  price line per mover, and an alert history. Each card has one-click searches
  for eBay sold listings, live eBay listings and Facebook Marketplace.
- **Excel workbook** (`tracker.xlsx`), replaced every day at a fixed link:
  `https://github.com/harrywkeppler/pokemon-card-tracker/releases/download/latest/tracker.xlsx`.
  Sheets: Alerts, Watch list, Top gainers, Top decliners, All cards, Alert
  history, Price history (60 days), About. To have Excel update it for you, see
  "Excel that refreshes itself" below.
- **`REPORT.md`**: the same alerts as a plain table. It renders on the
  repository's front page and in the GitHub mobile app.
- **`data/`**: the raw history (one small file per day), yours to keep.

## Set-up (about 10 minutes, once)

1. Create a free account at github.com, then a new **public** repository named
   `pokemon-card-tracker`. (Public is what makes the free dashboard page
   possible. It only contains public price data.)
2. Put these files in it, keeping the folder layout, including the hidden
   `.github` folder.
3. Open the **Actions** tab, enable workflows if asked, pick **Daily card
   tracker**, and press **Run workflow**. The first run takes longer than later
   ones because it also loads 45 past days of prices, so alerts work from day one.
4. Open **Settings → Pages**, choose **Deploy from a branch**, branch `main`,
   folder `/docs`, and save. Your dashboard appears at
   `https://YOUR-USERNAME.github.io/pokemon-card-tracker/`.

After that it runs by itself every day at 21:37 UTC.

If the last step of a run fails with a permissions error, open **Settings →
Actions → General → Workflow permissions** and choose **Read and write**.

## Excel that refreshes itself

Download the link above any time for that day's copy. Or set Excel to pull it
(Microsoft 365 Excel, Windows or Mac):

1. In a new, blank workbook: **Data → Get Data → From Other Sources → From Web**.
2. Paste the download link and press OK.
3. In the Navigator, tick **Select multiple items**, choose the tables you want
   (`tblAlerts`, `tblCards`, `tblPrices`, ...) and press **Load**.
4. **Data → Refresh All** fetches the latest day. To make it automatic: **Data →
   Queries & Connections**, right-click a query → **Properties** → tick
   **Refresh data when opening the file**.

You can add your own sheets, notes and charts next to the loaded tables; a
refresh only replaces the tables themselves. New data appears once a day,
around 4:40 pm Eastern.

## Where the numbers come from

| What | Source | Cost |
|---|---|---|
| Daily price of every card | TCGplayer market price via [tcgcsv.com](https://tcgcsv.com) | Free |
| Sales volume and graded eBay sales (optional) | [PokemonPriceTracker API](https://www.pokemonpricetracker.com/api) | Free for ~25 cards a day; paid tier for all cards |
| eBay sold / Facebook Marketplace | Search links on each card | Free |

Things to know:

- Prices are for **raw (ungraded) cards on TCGplayer**. TCGplayer's market price
  is built from completed sales, so it tracks eBay raw prices closely but is not
  the same number. Graded slabs are not in the free feed.
- eBay does not give individuals access to its sold-listings feed, and Facebook
  Marketplace has no sold prices and no data feed at all. That is why both are
  one-click checks rather than data sources.

## Turning on sales volume (optional)

1. Get a free key at pokemonpricetracker.com.
2. In the repository: **Settings → Secrets and variables → Actions → New
   repository secret**, name `PPT_API_KEY`, value = your key.

Each day the tracker then looks up units sold for the cards in `watchlist.csv`
first, then that day's alerts, then recently alerted cards, about 25 cards on the
free plan. A "volume spike" alert fires when 3-day sales are at least 3× a card's
usual pace; that needs about a week of stored volume for the card, so put cards
you care about in `watchlist.csv` (one TCGplayer product id per line; it is the
number in the card's TCGplayer link).

On a paid plan, raise `volume_api.daily_credits` in `config.json` to cover more
cards.

### Only cards with PSA 7–10 sales

With the key set, alerts are limited to cards that have graded sales (eBay and
Fanatics Collect) at **PSA 7 to 10**, and each alert shows the median sale price
per grade, for example `PSA 10 $1,250 (3) · PSA 9 $480 (5)`. The prices that
trigger an alert are still raw TCGplayer prices; the graded sales decide which
cards are kept.

| `graded_filter` setting | Default | Meaning |
|---|---|---|
| `enabled` | true | false shows every alert, graded sales or not |
| `grader` | `psa` | `psa`, `cgc`, `bgs`, `sgc` or `tag` |
| `min_grade` / `max_grade` | 7 / 10 | grade range, half grades included |
| `min_sales` | 1 | graded sales in range a card needs |

Each card looked up costs 3 credits, so the free plan (100 credits a day) checks
about 33 cards: the watchlist first, then the strongest alerts. An alert on a card
not checked that day is left out, since it cannot be shown to have PSA 7–10 sales.
The Pro plan (20,000 credits a day; set `volume_api.daily_credits` to 20000)
checks every tracked card. Without the key the filter cannot run: the report says
so and shows all alerts.

> This part was written from the provider's published field list and has not
> been run against the live service. The first reply is saved to
> `data/debug/ppt_last_response.json` so the parsing can be checked.

## How a card gets flagged

TCGplayer's market price only changes when a card sells, which for most cards
in this range is a few times a month. One sale can be an outlier (a graded copy
sold as raw, a misprint), so a price move only counts when the lowest or median
listing moved the same way, or two or more separate sales did. A single sale
that moves the price 3× or more while listings stay flat is ignored.

| Signal | Rule (all adjustable in `config.json`) |
|---|---|
| 7-day move | ±15% and ±$15 in a week, confirmed as above, and at least 2.5× that card's normal weekly swing |
| 1-day jump | ±10% and ±$15 in a day, with listings moving the same way |
| 30-day trend | ±30% in a month, from 2+ sales and matched by listings |
| Selling more often | price changed on 3+ of the last 7 days, at 3× the pace of the three weeks before (a free stand-in for sales volume) |
| Volume spike | 3-day units sold ≥ 3× usual (needs the volume key) |

- **High**: a 7-day move of 30%+ backed by both listings and repeat sales, or
  coming with a jump in sales activity.
- **Medium**: any other 7-day move, a volume spike, or more frequent sales
  together with a price signal.
- **Watch list** (hidden by default on the dashboard): a 30-day trend, 1-day
  jump, or more frequent sales on their own.

Each card also gets a **percentile rank** of its 7-day move against every other
tracked card that day (top and bottom 2% are tagged), and a **sale days** count:
days in the last 30 with a price change. Cards with 2 or fewer are tagged
"thinly traded".

## Settings (`config.json`)

| Setting | Default | Meaning |
|---|---|---|
| `band.min` / `band.max` | 100 / 5000 | Price range to track |
| `categories` | English + Japanese | Remove `"85"` to drop Japanese cards |
| `cards_only` | true | false also tracks sealed product (boxes, tins) |
| `thresholds.*` | see above | Alert sensitivity |
| `backfill_days` | 45 | Past days loaded on the first run |

## Running it yourself

```
pip install py7zr openpyxl
python -m tracker run       # download today's prices, then analyse
python -m tracker report    # re-analyse stored data only
python -m unittest discover -s tests -v
```

The test builds a simulated market with known planted moves, serves it locally
in tcgcsv's format, and checks the right cards are flagged.

Not financial advice.
