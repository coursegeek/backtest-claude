# IMPLEMENTATION_NOTES — Backtest V2 (clean-room)

Stan: **foundation/core + core portfolio engine** (sesja 3). Zaimplementowane: CLI i API
importu, konfiguracja, modele, kalendarz, dostępność informacji, loadery z normalizacją
kanoniczną, walidacja (semantyka luk Q-012), pipeline sygnałów, ledger, koszty transakcyjne,
cost basis (lots), centralny tygodniowy engine PORT-011 i komenda `run` dla
`tax.profile=none` + `portfolio.rebalance=signal-only`.
Nie ma jeszcze: podatków, rebalancingu kalendarzowego/band, sell_to_pay, terminal settlement,
metryk i `summary.csv`, `tax_events.csv`, scanów, optimize, tax-compare, walk-forward. Te
tryby rozwiązują i walidują config, po czym kończą się kodem 3 z jawnym komunikatem (bez
częściowych wyników).

Źródło prawdy dla statusów: `compliance_matrix.csv`; pytania: `implementation_questions.csv`.

## Uruchamianie

```bash
python -m pip install pytest pyyaml          # zależności: stdlib + PyYAML; pytest do testów
python -m pytest work                        # (pytest.ini: testpaths = tests)
python work/backtest.py signals --asset btc --as-of-date 2026-09-29
python work/backtest.py run --weights stocks=0.6,gold=0.2,btc=0.2 --start 2018-01-01 \
       --end 2026-07-31 --as-of-date 2026-09-29
python work/tools/check_audit_consistency.py --allow-pass
```

## Układ kodu

`work/backtest.py` (CLI + fasada importu) → `src/cli.py` → `src/app.py` (komendy).
Warstwy: `models`, `errors`, `config`, `calendar`, `availability`, `data_loader`, `validation`,
`signals`, `confirmation`, `scheduling`, `signal_analysis`, `allocation`, `rf`, `costs`,
`cost_basis`, `ledger`, `engine`, `manifest`, `reporting`. `src` jest pakietem importowanym jako `src.*` (Q-044).

## Decyzje implementacyjne

1. **Jednostki (Q-026)**: w configu wyłącznie ułamki dziesiętne; CLI `--threshold*`,
   `--threshold-grid`, gridy wag i `--max-drawdown-limit` w procentach; `--weights`,
   `--sell-fraction`, `--sortino-mar` dziesiętnie. Gridy zapisane w configu jako listy.
2. **Parametry sygnału (Q-027)**: najpierw warstwa (CLI > plik > command defaults >
   DEFAULTS), w warstwie specyficzność (`signals.<asset>` > `signal` > `signals.default`).
   Flagi sygnałowe CLI dotyczą `--asset`, aktywa `--single-asset`, a w wieloaktywowym `run`
   wszystkich aktywów ryzykownych. Defaulty per komenda: delay-scan `1:4`, threshold-scan
   `1:5:1` i delay `1`.
3. **Pliki danych (Q-001)**: domyślne nazwy ze specyfikacji; gdy nie istnieją w `data.dir`,
   stosowany jest jawny profil `work/config/cleanroom_data.yaml` (alias, adapter, zakres,
   deklarowane segmenty). Każde użycie aliasu: `resolved_via_alias` w manifeście + ostrzeżenie.
   Nazwa domyślna podana jawnie (CLI-005) też przechodzi przez alias.
4. **Adaptery i kanoniczność** (`Provenance.canonical`):
   * akcje: `week_start` (+4) lub `week_end`; segmenty z kolumny `source`, z deklaracji
     w profilu albo z kompozycji `compose_stock_signal` (Schwert < 1928 + SPX ≥ 1928,
     rebasing na ostatnim wspólnym tygodniu przed splice). `canonical` = spełnione SEM-001
     (`sem001_check`). Staged plik: segmenty 1962, `canonical=false`, ostrzeżenie (Q-004).
   * złoto: `lbma_weekly` (`week_end`), `lbma_daily` (agregacja ostatniego fixingu tygodnia,
     NORM-009) — kanoniczne; `tvc_oanda_proxy` tylko jawnie (profil), `canonical=false`,
     wiersze FILL oznaczone (Q-002). Kolumna `source` bez „LBMA” odbiera kanoniczność.
   * BTC: `canonical_friday` lub `raw_monday_week_start` (auto-detekcja); `week_key =
     source_week_start + 4`, `available_at = close_date = week_key + 2`; zakres kanoniczny
     z profilu 2011-07-08..2026-09-18 (794 wiersze), wiersze spoza zakresu w `excluded` (Q-005).
   * dywidendy: `canonical` wymaga pełnego SCHEMA-005; `shiller_proxy` jawnie, `canonical=false`
     (Q-008). Tryb exact: osobny plik `pay_date,dividend_return`; plik smoothed odrzucany (Q-036).
   * FF: CSV lub ZIP, filtr `^\d{8}$` + pola numeryczne, kolumny po nazwie, mapowanie NORM-013;
     `available_at` = data rekordu (sobota w okresie sesji sobotnich).
   * CPI: `previous_available` z flagą imputacji albo `error`.
5. **Kompletność tygodni (NORM-019 + Q-007)**: `week_key <= min(end, as_of)` i
   `available_at <= as_of`; domyślny `as_of` = dzisiejsza data Europe/Warsaw, zapisywana
   w `config_resolved.yaml` i manifeście.
6. **Luki kalendarza (Q-012, RESOLVED)**: `gap_issues` zawsze raportuje luki; brak
   automatycznego forward-fill; rekonstrukcja stanu na całej historii; liczniki confirmation
   restartują się po luce (flaga `counter_reset_gap`); `build_run_calendar` rozróżnia wspólną
   lukę (raport, tydzień pominięty) od braku jednego źródła (polityka `error|drop|carry`);
   carry tylko przy `missing.price_policy=carry` (flaga `carried`).
7. **SMA przez lukę (Q-050, RESOLVED)**: SMA to średnia ostatnich `ma` dostępnych obserwacji
   (bez forward-fill, bez zerowania historii SMA); luka ma flagi `calendar_gap` (zawsze),
   `counter_reset_gap` (gdy liczniki były niezerowe) i `sma_spans_gap` (okno SMA obejmuje lukę);
   warm-up liczy dostępne obserwacje przed startem.
8. **Warm-up (Q-013 propozycja)**: za mało historii → `WarmupError` (ERR-003); jawne
   `signal.initial_state=RISK_ON` zamienia błąd w ostrzeżenie.
9. **Stan początkowy (Q-019, RESOLVED)**: stan efektywny = wykonania zaplanowane przed pierwszym tygodniem;
   późniejsze pozostają w kolejce (`PreStartState.pending`). Brak potwierdzonej zmiany w historii
   → ostrzeżenie `initial_state_fallback` (SIG-003).
10. **Harmonogram (Q-043)**: FIFO, wykonanie przypadające na tydzień luki odbywa się w pierwszym
    obserwowanym tygodniu ≥ execution_week; wykonanie do bieżącego stanu to no-op (flaga).
11. **Tożsamość NAV (Q-049, RESOLVED)**: NAV zawsze liczony z komponentów (`math.fsum`
    w stałej kolejności); kontrola `abs(NAV - sum(sleeves) - rf_base)/max(1,|NAV|) <= 1e-10`
    oraz nieujemność i skończoność każdego komponentu po każdym kroku pipeline'u.
12. **Determinizm**: kanoniczna kolejność aktywów, `math.fsum` w SMA, `repr` dla floatów,
    YAML/JSON z sortowanymi kluczami; jedyne różnice między identycznymi runami to znacznik
    czasu w manifeście i nazwa katalogu wyników.

## Poprawki względem audytu

* Plik FF ma 7 linii nie-danych (audyt w TEST_PLAN/Q-021 liczył 8 przez pusty element po
  końcowym CRLF); poprawione.

## Silnik portfela (sesja 3)

13. **Ledger**: jawne składniki `stocks, gold, btc, rf_base, rf_reserve_stocks, rf_reserve_gold,
    rf_reserve_btc`; sleeve aktywa = aktywo + jego rezerwa RF (PORT-009).
14. **Koszty (Q-017, RESOLVED)**: RF jest ledgerem gotówkowym. `transaction_cost_bps` i
    `slippage_bps` dotyczą każdej realnej transakcji stocks/gold/btc (signal exit/re-entry, a w
    przyszłości rebalance, sell_to_pay, walk-forward rebalance, terminal liquidation); przesunięcie
    wartości do/z RF nie kosztuje. Sprzedaż: `net = traded*(1 - tc - slip)`; zakup z gotówki C:
    `traded = C/(1 + tc + slip)`, rezerwa po zakupie dokładnie 0. Początkowa alokacja nie jest
    transakcją, turnoverem ani kosztem.
15. **Transakcje**: `Trade` (models.py) z polami week_key, asset, side, reason, gross_traded_value,
    transaction_cost, slippage, net_cash_flow (znak z perspektywy składnika gotówkowego),
    asset/reserve before/after, cash_component, units, cost_basis, realized_gain, confirm_week,
    pipeline_step. Powody (`TradeReason`): signal_exit, signal_reentry oraz zarezerwowane
    calendar_rebalance, band_rebalance, sell_to_pay, walk_forward_rebalance, terminal_liquidation.
16. **Signal exit/re-entry**: exit sprzedaje `sell_fraction` bieżącej wartości pozycji
    (`target_fraction_of_sleeve`: do `(1-sell_fraction)` sleeve'a), wpływy tylko do własnej
    rezerwy; re-entry wydaje całą własną rezerwę. Brak kolejnych sprzedaży w RISK_OFF.
17. **Cost basis**: jednostki w indeksie ceny aktywa (1.0 na starcie, zmieniany zwrotem
    tygodniowym); loty initial/buy; koszt zakupu = wydana gotówka (z kosztami); sprzedaż
    zużywa loty FIFO (lub average_cost) i zwraca `Realization` (proceeds netto − basis). Brak
    podatku; dane dla przyszłego modułu podatkowego.
18. **Pipeline PORT-011** (`src/engine.py`): krok 0 `investable_capital`, 1 zaplanowane transakcje
    sygnałowe, 2 `amounts_due`, 3 `rebalance_or_fund`, 4 zwroty na wartościach po transakcjach,
    5 `immediate_taxes`, 6 sygnały końca tygodnia + `end_of_week`. Kroki 0/2/3/5/6 to metody
    `PipelineHooks`; moduły podatków/rebalancingu/sell_to_pay będą je nadpisywać i handlować przez
    prymitywy `WorkingPortfolio.sell` / `buy_with_cash`. Domyślny hook odrzuca niezerowe
    `amounts_due` (NotImplementedCommand) zamiast je ignorować. Inwarianty sprawdzane po każdym kroku.
19. **Zwroty**: stocks = (Mkt-RF+RF)/100 z FF tygodnia; gold/btc = cena bieżącego tygodnia
    kalendarza runu / cena poprzedniego tygodnia kalendarza runu (przez wspólną lukę: do ostatniej
    ceny); RF z FF dla `rf_base` i wszystkich rezerw (profil none: R_rf_net = R_rf; podatek RF
    trafi do kroku 5). Wagi użyte do zwrotu tygodnia T to wagi po transakcjach kroku 1
    (`weight_start_*` w weekly_portfolio.csv); sygnał z T działa najwcześniej przed zwrotem T+delay.
20. **Kalendarz runu**: wspólny zakres (NORM-011) ze wszystkich wymaganych źródeł z raportem obcięć
    (`range_truncated`), potem `build_run_calendar` (Q-012). Plik BTC ładowany tylko przy wadze
    BTC > 0 (DATA-005).
21. **Przygotowanie na Q-014/Q-015/Q-016**: `EngineInputs` przyjmuje jawny kalendarz i kapitał
    (mapowanie dat i inception pozostają w warstwie aplikacji); silnik jest czystą funkcją wejść
    i hooków, więc cienista symulacja pre-tax to drugi przebieg z innymi hookami; `WeekMarket`
    ma pole `dividend_yield`, a `CostBasisBook.open_lot` przyjmuje źródło lotu (np. przyszłe
    `dividend_reinvest`).

