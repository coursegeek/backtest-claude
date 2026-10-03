# IMPLEMENTATION_NOTES — Backtest V2 (clean-room)

Stan: **wszystkie cztery profile podatkowe (none, individual_pl, family_foundation_15/19 z
tax_event=terminal) w centralnym pipeline + terminal settlement + pre-tax shadow run + metryki +
summary.csv** (sesja 8), **komenda `tax-compare`** (sesja 9), **scany `delay-scan`,
`threshold-scan`, `rebalance-scan`** (sesja 10), **in-sample `optimize`** (sesja 11) **oraz
walk-forward `optimize --optimization-mode walk-forward`** (sesja 12). Zaimplementowane: CLI i API
importu, konfiguracja, modele, kalendarz, dostępność informacji, loadery z normalizacją
kanoniczną, walidacja (semantyka luk Q-012), pipeline sygnałów, ledger, koszty transakcyjne, cost
basis (lots), centralny tygodniowy engine PORT-011, rebalancing
`signal-only/weekly/monthly/quarterly/annually(yearly)/band`, sell_to_pay (TAX-006), finansowanie
należności przy rebalancingu (TAX-007), moduł podatkowy `individual_pl` (`src/tax.py`), moduł
fundacji (`src/foundation.py`), terminal settlement (`src/settlement.py`), pre-tax shadow run
(Q-015), moduł metryk (`src/metrics.py`), `summary.csv`, orkiestrator `tax-compare`
(`src/tax_compare.py`), scany (`src/scans.py`), optimizer in-sample (`src/optimizer.py`) i
walk-forward (`src/walk_forward.py`). Sesja 13 domknęła tanie MUST przed decyzją Q-037/Q-047
(CLI-001/007/008/009, TEST-020, TEST-023; Q-011/013/024/025/038 RESOLVED) bez nowej
funkcjonalności silnika.
Sesja 14 domknęła ostatnie MUST `IN_PROGRESS`: niezerowy internal trading tax fundacji (Q-047)
i `tax.foundation.tax_event=distribution_schedule` (Q-037). Nie ma jeszcze (SHOULD):
rolling_metrics.csv (MET-022/023), tabela konsolowa (REP-011), `spread_annual_dps` (DIV-009).
Jedynym niezdefiniowanym przypadkiem jest kombinacja `distribution_schedule` z niezerowym internal
trading tax (ConfigError, punkt 126). Sesja 15 zamknęła Q-004 i Q-008 wyłącznie nowymi danymi
kanonicznymi (sygnał akcji Schwert < 1928 + SPX ≥ 1928 i plik dywidend SCHEMA-005, punkty
129–137), bez zmian silnika. Wszystkie MUST poza jednym DATA_BLOCKER (SEM-003, złoto LBMA PM,
Q-002) są `PASS`.

Źródło prawdy dla statusów: `compliance_matrix.csv`; pytania: `implementation_questions.csv`.

## Uruchamianie

```bash
python -m pip install pytest pyyaml          # zależności: stdlib + PyYAML; pytest do testów
python -m pytest work                        # (pytest.ini: testpaths = tests)
python work/backtest.py signals --asset btc --as-of-date 2026-09-29
python work/backtest.py run --weights stocks=0.6,gold=0.2,btc=0.2 --start 2018-01-01 \
       --end 2026-07-31 --as-of-date 2026-09-29
python work/backtest.py run --weights stocks=0.6,gold=0.2,btc=0.2 --rebalance band \
       --rebalance-band-pp 1 --start 2018-01-01 --end 2026-07-31      # S07 / CLI-009
python work/backtest.py run --weights stocks=0.6,gold=0.2,btc=0.2 --tax-profile individual_pl \
       --rebalance band --rebalance-band-pp 1 --start 2018-01-01 --end 2026-07-31
python work/backtest.py tax-compare --config work/configs/tax_compare_s06.yaml \
       --tax-profile none,individual_pl,family_foundation_15,family_foundation_19   # S06 / CLI-006
python work/backtest.py delay-scan --asset stocks --start 1971-01-01 --end 2026-07-31 --ma 50 \
       --threshold 3 --confirm-weeks 2 --delay 1:4 --sell-fraction 0.5               # S02 / CLI-002
python work/backtest.py threshold-scan --asset stocks --start 1971-01-01 --end 2026-07-31 \
       --ma 50 --threshold 1:5:1 --confirm-weeks 2 --delay 1                          # S03 / CLI-003
python work/backtest.py rebalance-scan --weights stocks=0.6,gold=0.2,btc=0.2 --band-pp 1,5 \
       --start 2018-01-01 --end 2026-07-31                                           # S08 / CLI-010
python work/backtest.py optimize --start 2018-01-01 --end 2026-07-31 --btc-weight 0:25:1 \
       --gold-weight 0:25:1 --stocks-weight remainder --objective cagr               # S04 / CLI-004
python work/backtest.py optimize --start 2018-01-01 --end 2026-07-31 --btc-weight 0:25:1 \
       --gold-weight 0:25:1 --stocks-weight remainder --objective after_tax_cagr \
       --tax-profile individual_pl --dividend-tax-mode smoothed_weekly \
       --dividend-file SPX_dividend_return_weekly_1970_2026.csv                       # S05 / CLI-005
python work/backtest.py optimize --optimization-mode walk-forward \
       --optimize-params weights,ma,threshold,delay --walk-forward-window rolling \
       --train-years 15 --test-years 5 --ma-grid 40,50,60 --threshold-grid 0,1,3,5 \
       --delay-grid 1:4        # CLI-011: na danych staged kod 1 "insufficient history ..." (Q-020)
python work/tools/check_audit_consistency.py --allow-pass
```

## Układ kodu

`work/backtest.py` (CLI + fasada importu) → `src/cli.py` → `src/app.py` (komendy).
Warstwy: `models`, `errors`, `config`, `calendar`, `availability`, `data_loader`, `validation`,
`signals`, `confirmation`, `scheduling`, `signal_analysis`, `allocation`, `rf`, `costs`,
`cost_basis`, `ledger`, `engine`, `rebalancing`, `sell_to_pay`, `tax`, `foundation`, `settlement`,
`metrics`, `manifest`, `reporting`, `tax_compare`, `scans`, `optimizer` i `walk_forward`
(orkiestratory nad `app.prepare_run` / `app.run_prepared` / `engine.run_engine`). `src` jest pakietem importowanym jako `src.*` (Q-044).

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
     (`sem001_check`). Domyślny plik kanoniczny (sesja 15): kolumny `source`/`source_date`,
     Schwert 1920-01-02..1927-12-30 + SPX 1928-01-06..2026-09-25, `canonical=true` (punkt
     130). Staged plik (tylko jawnie, `work/config/staged_proxy_data.yaml`): segmenty 1962,
     `canonical=false`, ostrzeżenie (historia Q-004).
   * złoto: `lbma_weekly` (`week_end`), `lbma_daily` (agregacja ostatniego fixingu tygodnia,
     NORM-009) — kanoniczne; `tvc_oanda_proxy` tylko jawnie (profil), `canonical=false`,
     wiersze FILL oznaczone (Q-002). Kolumna `source` bez „LBMA” odbiera kanoniczność.
   * BTC: `canonical_friday` lub `raw_monday_week_start` (auto-detekcja); `week_key =
     source_week_start + 4`, `available_at = close_date = week_key + 2`; zakres kanoniczny
     z profilu 2011-07-08..2026-09-18 (794 wiersze), wiersze spoza zakresu w `excluded` (Q-005).
   * dywidendy: `canonical` wymaga pełnego SCHEMA-005 (domyślny plik kanoniczny od sesji 15,
     punkt 132); `shiller_proxy` jawnie, `canonical=false` (historia Q-008). Tryb exact:
     osobny plik `pay_date,dividend_return`; plik smoothed odrzucany (Q-036).
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
8. **Warm-up (Q-013, RESOLVED w sesji 13)**: za mało historii → `WarmupError` (ERR-003); jawne
   `signal.initial_state=RISK_ON` zamienia błąd w ostrzeżenie (szczegóły: punkt 112).
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
    `PipelineHooks`; moduły podatków/rebalancingu/sell_to_pay nadpisują je i handlują przez
    prymitywy `WorkingPortfolio.sell` / `buy` / `buy_with_cash` / `pay` / `transfer` (sesja 4). Domyślny hook odrzuca niezerowe
    `amounts_due` (NotImplementedCommand) zamiast je ignorować. Inwarianty sprawdzane po każdym kroku.
19. **Zwroty**: stocks = (Mkt-RF+RF)/100 z FF tygodnia; gold/btc = cena bieżącego tygodnia
    kalendarza runu / cena poprzedniego tygodnia kalendarza runu (przez wspólną lukę: do ostatniej
    ceny); RF z FF dla `rf_base` i wszystkich rezerw (profil none: R_rf_net = R_rf; podatek RF
    trafi do kroku 5). Wagi użyte do zwrotu tygodnia T to wagi po WSZYSTKICH transakcjach i
    płatnościach początku tygodnia, czyli po kroku 3 (`ledger_before_returns`,
    `weight_start_*` w weekly_portfolio.csv, sesja 4); sygnał z T działa najwcześniej przed
    zwrotem T+delay.
20. **Kalendarz runu**: wspólny zakres (NORM-011) ze wszystkich wymaganych źródeł z raportem obcięć
    (`range_truncated`), potem `build_run_calendar` (Q-012). Plik BTC ładowany tylko przy wadze
    BTC > 0 (DATA-005).
21. **Przygotowanie na Q-014/Q-015/Q-016**: `EngineInputs` przyjmuje jawny kalendarz i kapitał
    (mapowanie dat i inception pozostają w warstwie aplikacji); silnik jest czystą funkcją wejść
    i hooków, więc cienista symulacja pre-tax to drugi przebieg z innymi hookami; `WeekMarket`
    ma pole `dividend_yield`, a `CostBasisBook.open_lot` przyjmuje źródło lotu (np. przyszłe
    `dividend_reinvest`).

## Rebalancing strategiczny i sell_to_pay (sesja 4)

22. **Oś czasu portfela (Q-012)**: `EngineInputs.weeks` to zachowany kalendarz runu. Tydzień
    usunięty przez `missing.return_policy=drop` (lub wspólna luka) nie jest tygodniem portfela:
    engine w kroku 6 obserwuje wyłącznie punkt sygnałowy o `week_key` bieżącego zachowanego
    tygodnia; wcześniejsze punkty są pomijane i raportowane (`EngineResult.skipped_signal_observations`,
    `data_manifest.json`). Pomijana obserwacja nie może więc wytworzyć confirmation użytej przez
    engine, a liczniki przerywa luka kalendarza. Komenda `signals` (analiza sygnałów) jest bez
    zmian i nadal używa wszystkich obserwacji. Wykonanie zaplanowane nominalnie na tydzień
    usunięty wykonuje się w kroku 1 następnego zachowanego tygodnia; `Trade.nominal_execution_week`
    (kolumna trades.csv) zachowuje tydzień nominalny.
23. **Migawki ledgera**: `WeekRecord.ledger_start`, `ledger_after_signal` (po kroku 1),
    `ledger_before_returns` (po krokach 2–3), `ledger_end`; `nav_after_signal` i
    `nav_before_returns` w weekly_portfolio.csv obok `nav_start/nav_end`, plus `amounts_due`,
    `payments`, `rebalance`. Stan po sygnale pozostaje w audycie.
24. **Hooki**: `amounts_due(ctx, pf)`, `rebalance_or_fund(ctx, pf, due)`,
    `immediate_taxes(ctx, pf, ledger_before_returns, market)`, `end_of_week(ctx, pf)`; `ctx` to
    `WeekContext` (tydzień, poprzedni zachowany tydzień, indeks, targety, parametry, stany
    efektywne i potwierdzone po kroku 1). Engine po kroku 3 sprawdza, że płatności tygodnia
    sumują się do `amounts_due` (tolerancja 1e-9 względnie).
25. **Triggery (Q-039, RESOLVED)**: kalendarz — pierwszy zachowany rekord z Friday week_key w
    nowym okresie względem poprzedniego zachowanego rekordu (nie week_start, nie source_date);
    pierwszy rekord runu to alokacja początkowa, nie rebalance; `weekly` = każdy zachowany
    tydzień od drugiego; `yearly` jest aliasem `annually`. Band — na końcu tygodnia T (po kroku 5,
    w hooku kroku 6) `max |w_actual - w_target| >= band_pp/100 - 1e-12` po sleeve'ach stocks,
    gold, btc (aktywo + rezerwa) oraz rf_base (strategiczny sleeve RF); `band_pp` w punktach
    procentowych. Trigger czeka na następny zachowany tydzień (przez tygodnie usunięte);
    `RebalanceEvent` zapisuje `trigger_source_week`, `nominal_execution_week` (T+7 dni) i faktyczny
    `week_key`. Trigger z ostatniego tygodnia runu nie ma tygodnia wykonania i przepada (terminal
    settlement nie jest zaimplementowany).
26. **Plan rebalancingu (TAX-007, REB-008, REB-009)**: `NAV_net_for_rebalance = NAV_after_signal -
    amounts_due`. Udział aktywa w sleeve'ie `s_a` = 1 dla RISK_ON, bieżący `asset/sleeve` po kroku 1
    dla RISK_OFF (pusty sleeve: `1 - sell_fraction`). Końcowy NAV `F` spełnia dokładnie
    `F + c·Σ|s_a·w_a·F - A_a| = NAV_after_signal - amounts_due` (c = tc + slippage; transfery RF
    darmowe); lewa strona jest ściśle rosnąca i kawałkami liniowa, więc F wyznaczany jest
    dokładnie na odcinku między posortowanymi punktami załamania (rozwinięcie propozycji Q-018,
    które pozostaje formalnie OPEN). Cele: aktywo `s_a·w_a·F`, rezerwa `(1-s_a)·w_a·F`,
    rf_base `w_rf·F`. Sumy przez `math.fsum` (dokładnie zaokrąglone ⇒ niezależne od kolejności
    kluczy), wykonanie w kanonicznej kolejności stocks, gold, btc. `F < 0` ⇒ `InsolvencyError`.
27. **Wykonanie (PORT-011 krok 3)**: sprzedaże (do rf_base) → zwolnienia nadwyżek rezerw
    (transfer RF, bez kosztu) → płatność `amounts_due` z rf_base (`Payment`, kontekst
    `strategic_rebalance`) → zakupy (z rf_base, koszt `traded·c`) → uzupełnienia rezerw (transfer
    RF). Każda transakcja aktualizuje ledger, cost basis i realizację (sprzedaż) i trafia do
    trades.csv z powodem `calendar_rebalance` / `band_rebalance`. Stan sygnału się nie zmienia.
    Po kroku 3 wagi sleeve'ów równają się targetom (≤1e-12) na rzeczywistym NAV =
    NAV_after_signal − amounts_due − koszty − slippage. Gdy tydzień ma trigger, sell_to_pay nie
    jest używany.
28. **sell_to_pay (TAX-006, Q-046 RESOLVED; `src/sell_to_pay.py`)**: tylko bez triggera i przy
    `amounts_due > 0`: (A) rf_base; (B) rezerwy RF pro rata do bieżących wartości; (C)
    stocks/gold/btc pro rata do wartości rynkowych, brutto `N/(1-c)` ograniczone do pozycji,
    powód `sell_to_pay`, koszty, cost basis, realizacja; (D) `InsolvencyError`, gdy wartość
    likwidacyjna netto (`rf + rezerwy + (1-c)·aktywa`) < należność. Nic nie staje się ujemne.
    Stan sygnału bez zmian — jeśli (B) zużyje rezerwę sleeve'u w RISK_OFF, późniejsze re-entry
    nie ma gotówki i nie tworzy transakcji (RISK-005: tylko własna rezerwa).
29. **Zdarzenia audytu**: `Payment(week_key, event_type, amount, pipeline_step, funding_source,
    context)` — wypływ z NAV, nie transakcja i nie „podatek” z nazwy; `RfTransfer` — darmowe
    przesunięcie między składnikami RF; `RebalanceEvent` — NAV po sygnale, amounts_due, NAV netto,
    NAV planowany i zrealizowany, koszty, wagi przed/po, odchylenie band. Pliki: `payments.csv`,
    `rf_transfers.csv`, `rebalance_events.csv` (obok weekly_portfolio/trades).
30. **Przygotowanie Q-016** (sesja 4; od sesji 5 rozstrzygnięte, pkt 36):
    `WeekMarket.unit_price_return(asset)` jest jedynym miejscem, które przesuwa cenę jednostki
    cost basis.
31. **S07 (CLI-009)** na staged danych: `python work/backtest.py run --weights
    stocks=0.6,gold=0.2,btc=0.2 --rebalance band --rebalance-band-pp 1 --start 2018-01-01 --end
    2026-07-31` kończy się kodem 0: 448 tygodni, 170 zdarzeń `band_rebalance` (każde przekroczenie
    ≥1 pp na końcu T wykonane w T+1, wagi po kroku 3 = 60/20/20), 16 signal_exit, 14
    signal_reentry. To dowód mechaniki, nie zgodności danych: złoto/akcje to staged proxy
    (Q-002/Q-004), a `summary.csv` (metryki) nie istnieje, więc CLI-009 pozostaje IN_PROGRESS.

## Podatki individual_pl (sesja 5)

32. **Adjudykacje**: Q-014, Q-015, Q-016, Q-018, Q-029, Q-035 RESOLVED (treść w
    implementation_questions.csv i AUDIT_REPORT §12). Nowe pytania: Q-051 (dosłowna formuła
    daniny z external base powyżej progu), Q-052 (semantyka `actual_only` vs `error_on_estimate`).
    Q-030 RESOLVED w sesji 6 (formuła z pkt 35).
33. **Moduł `src/tax.py`** (TAX-001): `TaxParams` (stawki z `tax.individual.*`,
    `zero_rates()` dla przyszłego shadow runu Q-015), `TaxEvent`, `LossBucket`,
    `AnnualLiability`, `TaxState` (realizacje per rok, koszyki, wygasłe straty, zobowiązania
    roczne, sumy zapłacone, audyt zdarzeń; `copy()`/`to_dict()` - przenoszalny między oknami
    walk-forward) i `IndividualTaxHooks` (kroki 2, 5, 6). Engine, ledger, rebalancing i
    sell_to_pay nie zawierają reguł podatkowych; `engine.ComposedHooks(funding, *extensions)`
    łączy politykę kroku 3/6 (StrategicHooks) z rozszerzeniami (podatki; później fundacja).
34. **Rok podatkowy (Q-029)**: każda `Realization` (signal exit, rebalance, sell_to_pay, później
    terminal) trafia do roku swojego Friday week_key. W kroku 2 pierwszego zachowanego tygodnia
    roku Y+1 zamykane są wszystkie wcześniejsze otwarte lata (zwykle jeden): netting
    stocks+gold+btc (bez mark-to-market, bez dywidend i RF), koszyki strat, CG i danina.
    Transakcje kroku 1 tego tygodnia należą już do Y+1; sprzedaże sell_to_pay z kroku 3 też.
    Ostatni (otwarty) rok runu rozlicza terminal settlement (sesja 6, pkt 43-50).
35. **Koszyki strat (IND-009/010/013)**: roczna strata netto roku Y tworzy koszyk używalny w
    Y+1..Y+N (N = `loss_carryforward_years`, domyślnie 5), zużywany oldest-first; przy dodatnim
    zysku `eligible_offset = min(suma pozostałych niewygasłych sald * loss_offset_fraction,
    zysk)`; `taxable = max(0, zysk - offset)`; po zamknięciu roku Y+N koszyk wygasa (zapis w
    `expired_losses`).
36. **CG i danina (IND-001..005, IND-018)**: `CG = taxable * capital_gains_rate`;
    `solidarity_base = max(0, taxable) + external_solidarity_base_pln`, `solidarity =
    solidarity_rate * max(0, base - threshold)` - dosłownie (Q-051). Dwa osobne TaxEvent
    (settlement=annual, pipeline_step=2, tax_year=Y, także z kwotą 0 - pełny audyt każdego
    zamkniętego roku) i dwie osobne pozycje `AmountsDue` (`capital_gains_tax`,
    `solidarity_tax`, tylko > 0). Płatność wyłącznie w kroku 3: z wpływów rebalancingu (TAX-007),
    jeśli jest trigger, inaczej TAX-006 sell_to_pay; `Payment.event_type` = typ podatku. Hook
    kroku 6 uzgadnia płatności z zobowiązaniami (`paid_week`) i sumy zapłacone.
37. **Dywidendy (DIV-001..008, Q-016)**: tylko dostarczony `dividend_return` (nigdy FF − SPX).
    Krok 4: ledger akcji rośnie o `R_total` (ekonomia brutto), cena jednostki o
    `R_total − d`. Krok 5: `gross = stocks_before_returns * d`, `tax = gross * dividend_rate`
    potrącony z wartości akcji (`WorkingPortfolio.settle_dividend`), netto otwiera lot
    `dividend_reinvest` (koszt = netto, jednostki po bieżącej cenie) - `DividendReinvestment`, nie
    `Trade`: bez kosztów, slippage, turnover i trade_count. Po kroku 5
    `stock_end = stock_start * (1 + R_total − d*rate)`. Dywidendy nierozliczone przez hook
    (np. profil none z danymi) engine reinwestuje brutto (stawka 0). Po kroku 5 engine sprawdza
    `units * unit_price == wartość` każdego aktywa. Tryby: `smoothed_weekly` (plik tygodniowy,
    wymagane źródło zakresu NORM-011 i każdego tygodnia runu, brak => missing.return_policy),
    `exact` (plik cash-date Q-036, dywidenda w tygodniu pay_date, inne tygodnie 0), `off` (brak
    podatku, cena jednostki o zwrot całkowity). DIV-011 wg Q-052; status trafia do
    `tax_events.source_status`. Staged plik dywidend (proxy, wszystkie wiersze `estimate`)
    pozostaje DATA BLOCKER Q-008 - dowody mechaniki pochodzą z syntetycznych fixture'ów.
38. **RF (IND-014/015, PORT-013, RISK-006)**: dla rf_base i każdej rezerwy `income =
    max(0, value_before_returns * R_rf)`, `tax = income * rf_interest_rate` potrącony z tego samego
    składnika w kroku 5 (`WorkingPortfolio.withhold`, bez Payment i bez sell_to_pay); ujemny RF
    nie tworzy podatku ani ulgi; równoważne `R_rf_net = R_rf − max(R_rf, 0) * rate`.
39. **Kolejność kroku 5**: podatki bieżące po zwrotach kroku 4 i przed krokiem 6; nie zmieniają
    ekspozycji zwrotu tygodnia (`ledger_before_returns`), zmieniają wagi końca tygodnia, więc
    mogą wywołać przyszły trigger band (`tests/unit/test_tax.py::test_immediate_taxes_step5_timing`).
40. **Bez podwójnego zapisu przepływów**: podatek roczny = TaxEvent (ustalenie) + AmountsDue +
    Payment (odpływ w kroku 3); podatek od dywidend = TaxEvent + potrącenie z akcji w kroku 5;
    podatek RF = TaxEvent + potrącenie ze składnika RF. `weekly_portfolio.taxes_paid` =
    płatności kategorii tax + podatki tygodniowe danego tygodnia (kolumny `annual_tax_paid`,
    `dividend_tax`, `rf_interest_tax`), a `Σ taxes_paid = Σ tax_events(category=tax) =
    TaxState.total_tax_paid()` (TEST-024 w zakresie istniejących zdarzeń).
41. **Wyjścia**: `tax_events.csv` (pola Q-035 + TAX-004: date, tax_base, tax_due, asset,
    component, pipeline_step, source_status), `realizations.csv`, `dividend_reinvestments.csv`,
    `tax_state.json` (parametry, stan, otwarty rok), weekly_portfolio z `nav_after_returns`,
    dywidendami i podatkami; manifest z `inception_date` i `elapsed_days` (Q-014).
42. **Run na staged danych** (`--tax-profile individual_pl --rebalance band --rebalance-band-pp 1
    --transaction-cost-bps 10`, 2018-01-01..2026-07-31): kod 0; koniec zakresu obcięty do
    2026-06-26 (koniec staged pliku dywidend, ostrzeżenia NORM-011), 443 tygodnie, zdarzenia
    dividend_tax/rf_interest_tax co tydzień, CG i danina dla lat 2018-2025, rok 2026 otwarty;
    Σ zdarzeń = Σ taxes_paid = total_tax_paid. Tylko dowód mechaniki (Q-002/Q-004/Q-008).

## Terminal settlement individual_pl (sesja 6)

43. **Adjudykacje**: Q-030, Q-051, Q-052 RESOLVED; Q-032 rozstrzygnięty dla individual_pl (a w
    sesji 7 dla none: brak settlementu, `PortfolioRunResult.terminal is None`), OPEN tylko dla
    fundacji.
44. **Oddzielna warstwa** (`src/settlement.py`, PORT-014, IND-017): terminal settlement nie jest
    tygodniem backtestu. Engine kończy się `EngineResult.final_snapshot` - niemutowalnym
    `PortfolioSnapshot` (frozen Ledger, CostModel, krotki frozen Lot z `next_lot_id`, ceny
    jednostek); `WorkingPortfolio.from_snapshot()` odtwarza z niego nowy portfel z pustymi
    dziennikami (bez rekonstrukcji cost basis z wartości), `snapshot()` daje dokładny round trip.
    Rozliczenie działa na tym klonie i na `TaxState.copy()`; `EngineResult.weeks`, transakcje,
    płatności i tygodniowe TaxEvents nie są zmieniane, nie powstaje rekord tygodniowy ani zwrot.
    weekly_portfolio.csv kończy się `nav_end == pre_terminal_nav`; terminalne koszty/podatki nie
    wchodzą do ścieżki NAV, zwrotów ani drawdownu.
45. **Kolejność** (IND-016, IND-020): likwidacja 100% stocks/gold/btc w kolejności kanonicznej
    (`reason=terminal_liquidation`, `phase=terminal`, `pipeline_step` puste, `week_key` =
    ostatni zachowany Friday), koszty i slippage wg REB-003/004, cost basis FIFO/average_cost,
    `Realization`; wpływy netto do rf_base; składniki RF nie są handlowane. Realizacje trafiają do
    roku Friday week_key ostatniego tygodnia (otwartego roku), potem `close_tax_year(...,
    settlement="terminal")` - ta sama funkcja nettingu/koszyków/CG/daniny co przy zamknięciu
    rocznym (różni się tylko etykieta settlement, phase i brak kroku PORT-011).
46. **Płatność**: rezerwy RF są konsolidowane do rf_base darmowymi transferami
    (`terminal_consolidation`, phase terminal), a CG i danina płacone z rf_base jako `Payment`
    (`context=terminal_settlement`, phase terminal). Wynik nie zależy od kolejności kluczy aktywów
    (kolejność kanoniczna); brak gotówki => `InsolvencyError`, nigdy ujemny cash.
47. **TaxEvents terminalne**: `capital_gains_tax` i `solidarity_tax` z `settlement=terminal`,
    `phase=terminal`, `pipeline_step` puste, `week_key` = ostatni tydzień, `tax_year` = rok
    finalny, pełne podstawy i stawki; także z kwotą 0.
48. **`TerminalSettlementResult`** (pole `PortfolioRunResult.terminal`): pre_terminal_nav,
    liquidation_trades, terminal_transaction_costs, terminal_slippage, terminal_trading_costs,
    nav_after_liquidation, terminal/final-year realized gain, loss_offset, taxable_gain,
    terminal CG i danina, terminal_tax_total, after_tax_terminal_wealth, final_cash_ledger,
    final_liability, `tax_state_before_terminal` i `final_tax_state`. `breakout()` zawiera pola
    REP-017 dla individual_pl (terminal_foundation_tax = null).
49. **Wyjścia**: trades.csv / payments.csv / rf_transfers.csv / realizations.csv z kolumną
    `phase` (weekly | terminal; terminalne wiersze na końcu), tax_events.csv z kolumną `phase` i
    zdarzeniami terminalnymi, `terminal_settlement.json` (breakout + audyt likwidacji,
    zobowiązanie roku finalnego, płatności, konsolidacja), `tax_state.json` z `before_terminal`,
    `after_terminal` i `terminal`. `Σ weekly taxes_paid + terminal_tax_total =
    final total_tax_paid = Σ tax_events(category=tax)` (TEST-024 rozszerzony).
50. **Run na staged danych** (individual_pl, band 1 pp, 10+5 bps, 2018-01-01..2026-07-31, koniec
    obcięty do 2026-06-26): kod 0; 443 wiersze tygodniowe, ostatni `nav_end` = pre_terminal_nav;
    trzy terminalne sprzedaże, rok 2026 rozliczony terminalnie. Tylko dowód mechaniki
    (Q-002/Q-004/Q-008).

## Pre-tax shadow run, metryki i summary.csv (sesja 7)

51. **Adjudykacje**: Q-040 i Q-045 RESOLVED; Q-032 dla `tax.profile=none`: brak terminal
    liquidation i transakcji terminalnych, `pre_terminal_nav` = końcowy tygodniowy NAV, pola
    terminalne 0, `after_tax_terminal_wealth = pre_terminal_nav`.
52. **Shadow run (Q-015)** - `app.run_pre_tax`: dla individual_pl drugi przebieg centralnego
    silnika na tym samym obiekcie `EngineInputs` (te same tygodnie, obiekty WeekMarket, sygnały,
    parametry, targety, kapitał, koszty/slippage, konfiguracja rebalancingu, dywidendy, wynik
    polityki estimate i missing) z nową instancją polityki rebalancingu i
    `IndividualTaxHooks(params.zero_rates())`; dane nie są ponownie ładowane ani wyrównywane.
    Księgowość dywidend/cost basis działa jak w runie faktycznym, ale wszystkie AmountsDue są 0,
    więc nie ma sell_to_pay; ścieżki mogą się naturalnie rozjechać (podatki zmieniają NAV i
    triggery band). Shadow nie ma terminal settlement: `final_wealth_pre_tax` = końcowy NAV
    ścieżki shadow. Dla none pre-tax = przebieg faktyczny (`pre_tax_method=actual_run_no_taxes`);
    test regresyjny potwierdza identyczność osobnego shadow. `summary.csv` i manifest zapisują
    `pre_tax_method` (`shadow_zero_tax` | `actual_run_no_taxes`) i opis.
53. **`src/metrics.py`** (czyste funkcje, bez I/O): `cagr`, `annualized_volatility`, `sharpe`,
    `sortino`, `nav_path`, `drawdowns`, `max_drawdown`, `calmar`, `calendar_year_returns`,
    `best_and_worst_year`, `trade_count`, `turnover`, `risk_state_shares`, `rf_after_tax`,
    `real_cagr`, `cpi_window` i `compute_run_metrics` -> `RunMetrics`. Konwencje: CAGR
    `(NAV_end/NAV_start) ** (365.2425/elapsed_days) - 1`, elapsed_days = ostatni zachowany
    tydzień - inception (Q-014); zmienność = std(ddof=1)*sqrt(52); Sharpe = mean(R - RF) /
    std(ddof=1) * sqrt(52) (pre-tax RF brutto, after-tax individual_pl RF netto `R - max(R,0)*
    rf_interest_rate`, none RF); Sortino z MAR tygodniowym `(1+MAR)^(1/52)-1` i downside po
    wszystkich tygodniach; drawdown na `[NAV_start] + nav_end` (running max z NAV_start), nigdy z
    terminal settlement; Calmar = CAGR / maxDD pre-tax, after-tax Calmar = after-tax CAGR (z
    podatkiem terminalnym) / maxDD ścieżki after-tax (bez terminalu) - asymetria wymagana przez
    MET-010; lata wg Friday week_key (baza: NAV_start, potem NAV ostatniego tygodnia roku
    poprzedniego), pierwszy rok niepełny, jeśli run zaczyna się po jego pierwszym piątku,
    ostatni - jeśli kończy się przed ostatnim; trade_count i turnover (suma |gross| / średni
    nav_end, plus turnover_annualized) z transakcji fazy weekly; udziały RISK_ON/OFF per aktywo.
54. **Ścieżki**: pre-tax = shadow (`WeekRecord.portfolio_return` / `nav_end`), after-tax =
    przebieg faktyczny (podatki bieżące, płatności roczne, koszty); trading metrics z przebiegu
    faktycznego.
55. **CPI (Q-045, REAL-001..005)**: CPI ładowany po runie i nigdy nie blokuje wyników nominalnych
    (błąd => `real_cagr` puste + ostrzeżenie `cpi_unavailable`). CPI_start = miesiąc
    inception_date, CPI_end = miesiąc ostatniego zachowanego piątku; `cpi.mapping=
    previous_available` bierze ostatni wcześniejszy miesiąc (flaga `cpi_*_imputed`, ostrzeżenie
    `cpi_imputed_for_metrics`), znana luka 2025-10 jest wypełniana już przez loader. Etykieta z
    `cpi.label`; przy domyślnej `US CPI-U / CPIAUCNS` ostrzeżenie dokładnie `Real returns
    deflated by US CPI; not Polish CPI`, dla innej etykiety `Real returns deflated by <label>`.
56. **summary.csv** (REP-002/012/017/018, NORM-011): jeden szeroki wiersz, kolumny
    `reporting.SUMMARY_FIELDS` w stałej kolejności, bez timestampu (jest w katalogu i manifeście)
    - identyczne runy dają identyczne bajty. Zawiera: identyfikację runu i zakres (żądany,
    efektywny, inception, elapsed_days, tygodnie, wspólny zakres danych i każde obcięcie),
    majątek (pre-tax, pre_terminal_nav, after-tax), metryki pre-/after-tax, lata best/worst z
    flagą partial, trade_count, turnover, podatki per typ (CG i danina łącznie roczne +
    terminalne), rozbicie terminalne (dla none zera), koszty bps, tryb rebalancingu, CPI, as_of i
    porzucone tygodnie, targety, udziały RISK_ON/OFF per aktywo, parametry sygnału per aktywne
    aktywo, stawki zastosowane w runie i wszystkie założenia podatkowe/kosztowe configu (w tym
    parametry scenariusza fundacji - tylko jako założenia; profile fundacji nie są
    zaimplementowane). `reporting.summary_row` tylko mapuje wyniki, niczego nie liczy.
57. **Przykład** (staged, S07 band 1 pp, 10+5 bps, 2018-01-01..2026-07-31): none - CAGR =
    after-tax CAGR 0.1753, maxDD 0.2538, 540 transakcji; individual_pl (zakres do 2026-06-26 przez
    plik dywidend) - CAGR pre-tax 0.1751, after-tax 0.1469, maxDD pre 0.2538 / after 0.2978,
    podatki 516 673 w tym terminalny CG 129 401. Tylko dowód mechaniki (Q-002/Q-004/Q-008).

## Fundacje family_foundation_15/19, tax_event=terminal (sesja 8)

58. **Adjudykacje**: Q-032 (część fundacji), Q-033, Q-034 RESOLVED; Q-037 (distribution_schedule)
    i Q-047 (internal trading tax > 0) OPEN - oba przypadki kończą run `NotImplementedCommand`
    z jawnym wskazaniem pytania (nigdy nie są cicho zastępowane terminal / stawką 0).
59. **`src/foundation.py`**: `FoundationParams` (dividend 0.15, RF 0, internal 0, distribution
    rate z `tax.foundation_15|19.distribution_rate`, base, tax_event, setup 40 000, admin 40 000,
    proration; `zero_rates()` zeruje tylko podatki), `FoundationState` (setup/admin paid, admin
    per rok, zamknięte lata, dividend/RF/internal/distribution tax paid, realizacje per rok -
    audyt, zdarzenia; bez koszyków strat i CG; `copy()/to_dict()/totals()`) i `FoundationHooks`
    (kroki 0, 2, 5, 6). Z modułem individual_pl dzieli tylko `TaxEvent` i wspólny prymityw kroku 5
    `tax.charge_immediate_taxes` (Q-016: składnik cenowy, podatek od dywidendy, lot
    `dividend_reinvest`; RF per składnik, ujemny RF bez ulgi).
60. **Setup cost (FND-015/016, Q-033)**: krok 0 (`investable_capital`): investable = initial -
    setup, błąd gdy setup >= initial; zdarzenie `foundation_setup_cost` (category cost,
    settlement `initial`, pipeline_step 0, phase `initialization`, week_key = inception); nie
    jest Trade, kosztem transakcyjnym ani podatkiem. Metryki: `growth_base_nav` (CAGR, real CAGR)
    = initial capital, `path_start_nav` (ścieżka, drawdown, lata) = investable; summary:
    `initial_capital`, `investable_initial_capital`, `weekly_path_start_nav`, `growth_base_nav`,
    a `nav_start` oznacza bazę wzrostu (= growth_base_nav). Dla none/individual_pl wszystkie
    równe - wartości sprzed refaktoryzacji odtworzone bit w bit (test regresyjny).
61. **Koszt admin (FND-010..012/014, Q-034)**: dni roku Y w (inception, last_week]; prorated =
    40 000 * dni / 365|366, full = 40 000 za rok z aktywnym dniem. Otwarty rok startuje od roku
    inception, więc dni grudnia przed pierwszym tygodniem 1 stycznia są należne w kroku 2 tego
    tygodnia. Rok Y: AmountsDue `foundation_annual_admin_cost` w kroku 2 pierwszego zachowanego
    tygodnia Y+1, płatność w kroku 3 istniejącym mechanizmem (TAX-007 przy rebalancingu, TAX-006
    bez); zdarzenie category cost, settlement annual. Rok finalny w terminal settlement.
    `weekly_portfolio.costs_paid` pokazuje koszty tygodnia; nie wchodzą do `taxes_paid` ani
    `total_tax_paid`.
62. **Terminal settlement fundacji (Q-032)** - `settlement.settle_foundation_terminal` na kopiach
    snapshotu i stanu: likwidacja 100% stocks/gold/btc (`foundation_distribution_liquidation`,
    phase terminal, koszty i slippage, cost basis, realizacje - audyt, stawka internal 0) ->
    konsolidacja rezerw RF (darmowa) -> koszt admin roku finalnego (Payment + TaxEvent category
    cost, settlement terminal) -> `distributed_amount` = gotówka po kosztach likwidacji i koszcie
    admin -> podstawa `distributed_amount` albo `gain_only = max(0, distributed - initial_capital
    sprzed setup cost)` -> podatek `foundation_distribution_tax` (15% / 19%, category tax,
    settlement terminal) -> `after_tax_terminal_wealth = distributed - tax`. Brak CG i daniny.
    Brak gotówki na koszt => InsolvencyError. Summary: terminal_foundation_tax = terminal_tax_total
    = podatek od dystrybucji, terminal CG/danina 0, `distributed_amount`,
    `distribution_tax_base(_mode)`; `foundation_state.json` (before/after terminal) zamiast
    `tax_state.json`.
63. **Shadow pre-tax fundacji (Q-015)**: ten sam `EngineInputs`, `FoundationHooks(zero_rates())`
    (setup i koszty admin zachowane, podatki 0), potem `settle_foundation_shadow_costs`: koszt
    admin roku finalnego opłacony waterfallem TAX-006 (rf_base -> rezerwy -> aktywa pro rata z
    kosztami, phase terminal), bez pełnej likwidacji i bez podatku od dystrybucji;
    `final_wealth_pre_tax` = wartość po tym koszcie; nie jest częścią ścieżki ani drawdownu.
64. **Warstwy (FND-007)**: podatek od dywidend (tygodniowy) i od dystrybucji (terminalny) są
    niezależne; podstawa dystrybucji nie cofa zapłaconego podatku od dywidend.
65. **15 vs 19**: przy identycznym configu ścieżka tygodniowa, koszty, podatki od dywidend,
    transakcje i pre_terminal_nav są identyczne; różnią się tylko stawka, podatek od dystrybucji,
    majątek po podatku i metryki after-tax od niego zależne (test integracyjny).
66. **Przykład syntetyczny** (1 040 000, setup 40 000, akcje 80% / RF 20%, +50% w tygodniu 11,
    dywidenda 0.05%/tydz., 10+5 bps, inception 2021-12-31, 2022-01-07..2022-12-30): koszt admin
    2021 = 0 (0 dni), 2022 terminalny 39 890.41 (364/365), podatek od dywidend 4 341.28,
    pre_terminal_nav 1 400 625.68, koszty likwidacji 1 793.04, distributed_amount 1 358 942.23;
    fundacja 15%: podatek 203 841.33 -> 1 155 100.90 (gain_only: podstawa 318 942.23, podatek
    47 841.33); fundacja 19%: 258 199.02 -> 1 100 743.21 (gain_only 60 599.02); shadow: NAV
    tygodniowy 1 405 266.86 - koszt admin 39 890.41 z rf_base = final_wealth_pre_tax 1 365 376.44.

## tax-compare (sesja 9)

67. **Q-023 (RESOLVED)**: tax-compare jest pełnym portfolio runem - ALLOC-001 obowiązuje, wagi
    tylko z `--config` (allocation.targets) albo `--weights`; brak ukrytych wag i dat. Clean-room
    nie używa configów V1 (`configs/portfolio.yaml` z przykładu CLI-006 nie istnieje -> błąd
    "config file not found"). S06 ma zamrożony config V2 `work/configs/tax_compare_s06.yaml`
    (bez pojedynczego `tax.profile`); `AB_SCENARIOS.csv` S06 wskazuje ten config. Pojedynczy
    `tax.profile` w pliku configu tax-compare jest jawnym błędem (oś profilu to
    `--tax-profile` / `tax.compare_profiles`).
68. **Podział runu**: `app.prepare_run(cfg, dividend_mode)` ładuje, waliduje i wyrównuje dane raz
    (`PreparedRun`: niemutowalny `EngineInputs`, kalendarz, dropped weeks, zakres wspólny,
    obserwacje/status dywidend, okno CPI, proweniencja, wspólne issues walidacji, as_of);
    `app.run_prepared(cfg, prepared)` to produkcyjna ścieżka (engine + funding/rebalancing hooks
    + hooki profilu + terminal settlement + shadow pre-tax + metryki + wyjścia) bez ponownego
    ładowania. `run` = `run_prepared(cfg, prepare_run(cfg))`; wyniki normalnego `run` bez zmian
    (bajtowo identyczne pliki, poza nowymi kolumnami summary).
69. **Wspólny eksperyment**: lista profili walidowana (dozwolone wartości, bez duplikatów, >= 1),
    kolejność kanoniczna none, individual_pl, family_foundation_15, family_foundation_19
    (niezależnie od kolejności wejścia; wejście zapisane w manifeście). Config każdego profilu
    to nowy `ResolvedConfig` różniący się wyłącznie `tax.profile` (test porównuje rozwiązane
    configi). Wymagania danych profili są łączone (superset): jeśli choć jeden profil wymaga
    pliku dywidend, plik wchodzi do wspólnego kalendarza, więc także `none` ma ten sam
    effective_first_week / effective_last_week / weeks / dropped weeks. `none` na tym wejściu
    reinwestuje dywidendy brutto (Q-016, stawka 0) bez zdarzeń podatkowych. Samo `none` (bez
    innych profili) zachowuje własny, dłuższy kalendarz - jak `run`.
70. **Wyjścia**: `summary.csv` (wiersz na profil, dokładnie `SUMMARY_FIELDS` runu; `tax_profile`
    jest teraz pierwszą kolumną także w `run`), `tax_compare_manifest.json` (profile, shared_input,
    prepared_input_sha256 - hash wejścia widzianego przez engine, zakres, dropped weeks, źródła
    z SHA256, as_of, SHA configu i rozwiązanych parametrów strategii, kontrole wspólnego
    eksperymentu), wspólne `data_manifest.json`, `config_resolved.yaml` (bez pojedynczego
    tax.profile), `validation_report.csv`, `weekly_normalized.csv` oraz `profiles/<profil>/`
    ze standardowymi artefaktami runu; manifest profilu powtarza współdzielone hashe i wskazuje
    manifest wspólny (dane nie były ładowane ponownie).
71. **REP-012**: kolumny `applied_*` pokazują parametry faktycznie stosowane przez profil wiersza
    (stawki, próg i baza daniny, carry-forward, zdarzenie/podstawa podatku fundacji, setup i
    admin cost, proration; 0.0 lub `not_applicable` gdy profil ich nie stosuje); kolumny `tax_*`
    to skonfigurowane założenia scenariusza, identyczne w każdym wierszu.
72. **Inwarianty**: none.final_wealth_pre_tax == individual_pl.final_wealth_pre_tax (shadow
    individual bez podatków i bez kosztów fundacji = faktyczna ścieżka none);
    foundation_15.final_wealth_pre_tax == foundation_19.final_wealth_pre_tax; ścieżka tygodniowa
    15 == 19 (tygodnie, ledgery, transakcje, płatności, transfery, rebalancing, dywidendy,
    zdarzenia podatkowe/kosztowe, pre_terminal_nav). Nie oczekujemy none pre-tax == fundacja
    pre-tax (setup/admin cost także w shadow). Kontrole zapisane w manifeście (bez rankingu).
73. **Atomowość**: wszystkie configi profili walidowane przed ładowaniem danych (Q-037, Q-047,
    ALLOC-001); błąd profilu w trakcie runu (np. insolvency) przerywa komendę jako
    `TaxCompareError` z nazwą profilu i kodem wyjścia przyczyny; wyniki zapisywane dopiero po
    policzeniu wszystkich profili, a błąd zapisu usuwa katalog wyniku.
74. **Brak oceny**: komenda nie tworzy rankingu, zwycięzcy ani rekomendacji - tylko liczby
    profili obok siebie.
75. **Frozen S06 (staged data, walidacja mechaniki - Q-002/Q-004/Q-008 bez zmian)**:
    2018-01-05..2026-06-26 (443 tygodnie, inception 2017-12-29; koniec obcięty przez plik
    dywidend i wspólny zakres), wszystkie profile na tym samym kalendarzu; final_wealth_pre_tax
    none = individual_pl = 3 999 110.48, fundacje 15/19 = 3 114 471.16; after_tax_terminal_wealth:
    none 3 999 110.48, individual_pl 3 251 509.53, fundacja 15% 2 615 887.69, fundacja 19%
    2 492 787.09.

## Scany delay/threshold/rebalance (sesja 10)

76. **Q-026 (RESOLVED) - jednostki**: CLI `--threshold`, `--threshold-off`, `--threshold-on`,
    `--threshold-grid` i (przyszłe) gridy wag `--btc-weight`, `--gold-weight`, `--stocks-weight`,
    `--rf-weight` są w procentach: `--threshold 3` = 0.03, `--threshold 0.03` = 0.0003 (0.03%) -
    bez zgadywania intencji. Dziesiętnie: `--weights`, `--sell-fraction`, `--sortino-mar`. Punkty
    procentowe: `--rebalance-band-pp`, `--band-pp` (1 = 1 pp). Config YAML/JSON: progi zawsze
    jako ułamki dziesiętne (threshold-scan odrzuca grid z configu poza [0, 1)).
77. **Składnia gridów**: `a:b:step` inclusive (b należy do gridu, gdy trafiony dokładnie; bez
    step krok 1), listy nieregularne i mieszane (`1:3,8`); kolejność jak podana (bez
    sortowania). Parsowanie w arytmetyce `Decimal` (`config.parse_decimal_grid`): wartości to
    dokładne dziesiętne `start + i*step`, konwersja procent -> ułamek przez `Decimal / 100`, bez
    arbitralnego zaokrąglania (`0:1:0.1` daje 0.3, `7.5` -> 0.075, `1.1` -> 0.011). Gridy
    całkowite (delay, ma, confirmation) odrzucają wartości niecałkowite (1.5 -> błąd, nie
    obcięcie). Resolved grid zapisany w `config_resolved.yaml` (`optimizer.*_grid`),
    `scan_manifest.json` (`resolved_grid`, `grid_points`) i w każdym wierszu `grid_results.csv`.
78. **Q-027 (RESOLVED) - precedencja**: CLI > `signals.<asset>.*` > `signal.*` >
    `signals.default.*` > DEF-* (implementacja: warstwa CLI > plik > defaulty komendy > DEFAULTS,
    w warstwie specyficzność asset > signal > default; DEF-* są w warstwie DEFAULTS). Defaulty
    komend: delay-scan `optimizer.delay_grid` = 1:4; threshold-scan `optimizer.threshold_grid`
    = 1:5:1 (0.01..0.05) z delay = skalarny delay aktywa (default 1, THR-003) - threshold-scan
    nigdy nie używa `optimizer.delay_grid`; run delay = 1. Listy 1,2,4,8 i 0,1,2,3,5,7.5 są
    przykładami składni. W scanach `--ma`, `--threshold`, `--confirm-weeks`, `--delay`,
    `--sell-fraction` dotyczą tylko `--asset`.
79. **Architektura scanu** (`src/scans.py`): `resolve_scan` rozwiązuje i waliduje cały grid
    (unikalność, zakresy, całkowitość) i tworzy dla każdego punktu nowy `ResolvedConfig`
    (`ResolvedConfig.with_overrides`) różniący się od configu scanu wyłącznie skanowanym kluczem
    (sprawdzane porównaniem rozwiązanych configów); walidacja profilu (Q-037/Q-047) i aktywów
    przed danymi. Potem jedno `app.prepare_run` i `app.run_prepared` na punkt:
    `PreparedRun.inputs_for(cfg)` współdzieli obiekty danych (tygodnie, rynek, serie sygnałów) i
    podmienia tylko pola strategii (parametry sygnału, cele, kapitał, koszty); każdy punkt ma
    świeży portfel, cost basis, trackery sygnałów i stan podatkowy/fundacji (test: kolejność gridu
    nie zmienia wierszy; wiersz delay=N = samodzielny `run` z delay=N).
80. **Warm-up scanu (NORM-010/ERR-003)**: wymaganie = największe `ma + max(confirm_off,
    confirm_on) + delay` spośród punktów gridu (delay-scan: max(delay grid); threshold i band
    nie zmieniają warm-upu). Z `--start`: jeśli historia przed startem nie wystarcza dla
    największego wymagania, cały scan kończy się ERR-003 przed wykonaniem gridu (bez cichego
    przesuwania startu dla części punktów). Bez `--start`: pierwszy wspólny tydzień zakresu
    wspólnego, przed którym każde aktywne aktywo ma to wymaganie (`app.first_warmup_week`,
    `first_week_rule` w manifeście). Od sesji 13 ta sama reguła obowiązuje `run` bez `--start`
    (Q-025, punkt 113).
81. **delay-scan / threshold-scan (ALLOC-002)**: `--asset X` = strategia single-asset
    (`allocation.single_asset = X`, sleeve 100%, RF tylko rezerwa risk-off, strategic rf 0);
    `--weights`/`allocation.targets` z `--asset` to błąd (bez ukrytych wag); ładowane są tylko
    FF, ceny sygnałowe X, dywidendy (jeśli profil ich wymaga) i CPI. delay-scan zmienia wyłącznie
    `signals.X.delay_weeks` (wykonanie nominalnie w T+N, bez ukrytego +1); threshold-scan ustawia
    symetrycznie `threshold_off = threshold_on = p` (nadpisuje asymetryczne progi z configu;
    `--threshold-off/--threshold-on/--threshold-grid` w threshold-scan odrzucone), threshold 0
    zachowuje ścisłe porównanie.
82. **rebalance-scan (REB-010, ALLOC-001)**: pełny run wieloaktywowy z jawnymi wagami; każdy punkt
    wymusza `portfolio.rebalance = band` i `portfolio.rebalance_band_pp` = wartość gridu (pp);
    inne tryby rebalancingu nie są skanowane. `rebalance_count` = liczba faktycznych
    `RebalanceEvent`.
83. **grid_results.csv**: kolumny `scan_type, grid_index (1..n), asset, scanned_parameter,
    scanned_value, delay_weeks, threshold_pct, threshold_decimal, band_pp, status,
    rebalance_count, signal_exit_count, signal_reentry_count` (wartość skanowana w kolumnie
    swojego scanu, pozostałe puste; liczby wyjść/powrotów = wykonane zmiany stanu efektywnego),
    a po nich pełny `SUMMARY_FIELDS` runu punktu (pre-tax i after-tax, podatki, terminal
    settlement - DELAY-005). Jeden wiersz na punkt resolved gridu, kolejność gridu, `status=ok`;
    bez timestampu, celu, rankingu i „winnera”. Katalog scanu: `grid_results.csv`,
    `scan_manifest.json`, `config_resolved.yaml`, `data_manifest.json`, `validation_report.csv`
    (wspólne issues + issues runów oznaczone punktem gridu), `weekly_normalized.csv` (raz); bez
    plików tygodniowych/transakcji/podatków per punkt.
84. **prepared_input_sha256** (zmiana definicji, dotyczy też tax-compare): hash wyłącznie danych
    i kalendarza widzianych przez engine (tygodnie, zwroty, RF, dywidendy i status, serie cen
    sygnałowych, pierwszy tydzień, as_of, tryb dywidend, zakres wspólny, dropped weeks, okno CPI,
    SHA256 źródeł); parametry strategii (sygnał, wagi, kapitał, koszty, band, profil) są poza
    nim i zapisywane osobno. Dlatego delay-scan i threshold-scan tego samego aktywa/zakresu mają
    ten sam hash; wartość S06 tax-compare zmieniła się na `dd030c03…` (summary.csv bez zmian).
85. **Atomowość i postęp**: cały grid i configi walidowane przed danymi; błąd punktu w trakcie
    runu (np. insolvency) przerywa scan jako `ScanError` z numerem i wartością punktu; wyniki
    zapisywane po policzeniu wszystkich punktów, błąd zapisu usuwa katalog. Postęp (ERR-005) na
    stderr po każdym punkcie (`performance.progress`), nigdy w plikach wyników. REPRO-006
    pozostaje IN_PROGRESS do testu serial vs parallel optimizera.
86. **Wyniki (staged data, profil none, as_of = data uruchomienia)**: S02 delay-scan stocks
    1971-01-01..2026-07-31 (2901 tygodni, warm-up 56): delay 1/2/3/4 -> trade_count 34/34/34/35,
    final_wealth_pre_tax 380 631 514.50 / 344 205 781.02 / 331 693 022.82 / 320 914 731.13, CAGR
    11.28% / 11.08% / 11.00% / 10.94% (przy delay 4 dodatkowy powrót: wykonanie potwierdzenia z
    1970-12-04 oczekujące na starcie, Q-019). S03 threshold-scan 1..5%: trade_count
    54/40/34/30/30. S08 rebalance-scan 2018-01-05..2026-07-31 (448 tygodni): band 1 pp -> 170
    rebalancingów, band 5 pp -> 20.

## Optimizer in-sample (sesja 11)

87. **Q-041 (RESOLVED) - wagi**: `stocks = remainder` -> dla każdej kombinacji stocks = 1 - btc
    - gold - rf (arytmetyka dziesiętna na wartościach gridu); stocks < -1e-12 -> kombinacja
    odrzucona (`rejected_weight_sum_gt_1`), wartość w [-1e-12, 0) ustawiona na dokładne 0,
    poprawny remainder zawsze sumuje się do 1. Jawny grid stocks: sum = stocks + gold + btc + rf,
    kwalifikuje się tylko |sum - 1| <= 1e-12 (ALLOC-001); > 1 -> `rejected_weight_sum_gt_1`,
    < 1 -> `rejected_weight_sum_lt_1`; brakująca część nigdy nie trafia do RF. Jednostki
    (Q-026): `--btc-weight`, `--gold-weight`, `--stocks-weight` (grid), `--rf-weight` (stała,
    OPT-005) i `--max-drawdown-limit` w procentach; config dziesiętnie.
88. **Grid**: iloczyn kartezjański btc (zewnętrzny) x gold x stocks (tylko jawny grid), rf
    stały; każda kombinacja ma stabilny `grid_index` 1..n (niezależny od workerów, czasu i
    objective). Cały grid budowany i walidowany przed danymi; brak wagowo poprawnej kombinacji
    -> ConfigError bez ładowania danych. Odrzucone kombinacje zostają w `grid_results.csv` z
    wagami, statusem i powodem, z pustymi metrykami, i nigdy nie uruchamiają engine.
89. **Wspólny kalendarz (unia aktywów)**: unia aktywów o dodatniej wadze we wszystkich
    wagowo poprawnych kandydatach (`optimizer_manifest.required_assets_union`) jest
    przygotowana raz (`app.prepare_run(assets=union)`): każde źródło unii ogranicza jeden
    wspólny kalendarz, więc kandydat z btc = 0 nie dostaje dłuższego zakresu niż kandydat z
    btc > 0. `PreparedRun.inputs_for` tworzy EngineInputs kandydata (jego wagi, jego aktywne
    aktywa, ich SignalParams) na współdzielonych tygodniach, rynku i seriach sygnałów.
    Przykład: `--end 2013-12-31 --btc-weight 0,10 --gold-weight 0` -> oba kandydaty 2012-07-06
    ..2013-12-27 (78 tygodni, start po warm-upie BTC), ten sam `prepared_input_sha256`; kandydat
    btc = 0 przygotowany osobno startowałby 1926-07-02. Bez `--start` optimizer (jak scany)
    wybiera pierwszy wspólny tydzień z pełnym warm-upem unii.
90. **Objective (OPT-007)**: cagr -> `cagr`, after_tax_cagr -> `after_tax_cagr`,
    terminal_wealth -> `final_wealth_pre_tax` (pre-tax shadow z niepodatkowymi kosztami fundacji,
    nigdy `pre_terminal_nav`), after_tax_terminal_wealth -> `after_tax_terminal_wealth`, sharpe,
    sortino, calmar - maksymalizowane; min_drawdown -> `max_drawdown`, minimalizowane
    (`objective_value` pokazuje dodatni max_drawdown, bez sztucznego minusa). Metryka None/NaN
    -> `objective_unavailable` (nigdy 0), kandydat zostaje w gridzie, nie bierze udziału w wyborze.
91. **Limit drawdownu (OPT-008)**: relevant drawdown = `after_tax_max_drawdown` dla
    after_tax_cagr i after_tax_terminal_wealth, `max_drawdown` dla pozostałych; kwalifikuje
    się relevant <= limit (równy przechodzi); przekroczenie -> `rejected_drawdown_limit` z
    pełnymi metrykami. Brak kandydata eligible -> `OptimizerError` "no eligible optimization
    candidate" (kod 4), bez żadnych plików.
92. **Tie-break (OPT-009, Q-041)**: wybór = minimum krotki (objective zanegowany dokładnie dla
    celów maksymalizowanych, relevant max drawdown, turnover, (btc, gold, rf, stocks)); bez
    zaokrągleń i tolerancji - kolejne kryterium decyduje tylko przy dokładnej równości. Wybór
    wykonywany po złożeniu wszystkich wierszy w kolejności grid_index (niezależny od kolejności
    ukończenia, workera i wejścia; krotka wag jest unikalna, więc porządek jest ścisły).
93. **Wiersz grid_results.csv**: `grid_index, status, rejection_reason, eligible, selected,
    weight_stocks, weight_gold, weight_btc, weight_rf, weight_sum, objective,
    objective_direction, objective_metric, objective_value, relevant_drawdown_metric,
    relevant_max_drawdown, max_drawdown_limit` + pełne `SUMMARY_FIELDS` kandydata (puste dla
    odrzuconych wagowo). Statusy: ok, rejected_weight_sum_gt_1, rejected_weight_sum_lt_1,
    rejected_drawdown_limit, objective_unavailable. Dokładnie jeden `selected = true`.
94. **Wyjścia**: `grid_results.csv`, `summary.csv` (wiersz `SUMMARY_FIELDS` wybranego
    kandydata), `selected/` (standardowe artefakty wybranego runu z już policzonego
    `PortfolioRunResult` - bez ponownego runu), `optimizer_manifest.json` (objective, kierunek,
    tie-break, limit, liczniki raw/weight-valid/evaluated/eligible/rejected/selected, wybrany
    punkt, unia aktywów, hash, zakres, dropped weeks, as_of, jobs, źródła, kontrole),
    `config_resolved.yaml`, `selected_config_resolved.yaml`, `data_manifest.json`,
    `validation_report.csv` (wspólne issues + różne issues runów raz, z liczbą kandydatów),
    `weekly_normalized.csv`. Brak plików tygodniowych dla wszystkich kandydatów.
95. **Równoległość (ERR-006)**: `--jobs 1` sekwencyjnie, N > 1 pula procesów N, `-1` / `auto`
    (domyślnie, zgodnie ze spec ERR-006; wcześniej default 1) = dostępne CPU, `0` i `< -1` ->
    ConfigError; nigdy więcej workerów niż kandydatów. Dane przygotowane raz w procesie
    nadrzędnym; worker dostaje przygotowane wejście raz (initializer, `fork` gdy dostępny),
    nie czyta plików, nie buduje kalendarza, nie zapisuje plików i nie tworzy timestampów;
    zwraca wiersze kandydatów swojego bloku grid_index oraz pełny wynik najlepszego kandydata
    bloku (odłączony od współdzielonych danych i ponownie dołączony w procesie nadrzędnym) -
    wybrany kandydat jest zawsze najlepszym w swoim bloku, więc jego wynik jest dostępny bez
    ponownego runu. Proces nadrzędny składa wiersze po grid_index, wybiera i zapisuje;
    `grid_results.csv`, `summary.csv` i `selected/` są bajtowo identyczne dla jobs 1, 2, -1
    (różnią się tylko timestamp i pola jobs w manifeście oraz `performance.jobs` w configu).
    Postęp (ERR-005): "optimize: completed X/Y evaluated candidates" na stderr, w kolejności
    ukończenia, bez wpływu na wyniki.
96. **Wydajność (bez zmiany wyników)**: rekonstrukcja historii sygnału przed startem jest
    memoizowana (klucz: niezmienna krotka punktów, parametry sygnału, pierwszy tydzień;
    zwracana głęboka kopia trackera, więc żaden run nie współdzieli stanu mutowalnego); okno SMA
    trzyma ceny i licznik luk inkrementalnie (`math.fsum` bez zmian). Kandydat S04 ~0.05 s,
    S05 ~0.15 s. Wszystkie wcześniejsze wyniki (run x8, tax-compare S06, scany S02/S03/S08,
    delay-scan BTC z podatkami, signals x3) bajtowo identyczne poza `performance.jobs: auto`.
97. **Wyniki (staged data, mechanika)**: S04 (cagr, none): 676 kombinacji, wszystkie wagowo
    poprawne i eligible, wybrany grid_index 676 = stocks 0.50 / gold 0.25 / btc 0.25 / rf 0,
    2018-01-05..2026-07-31 (448 tygodni), final_wealth_pre_tax 3 862 305.31, CAGR 17.04%, max DD
    34.11%, turnover 3.9628. S05 (after_tax_cagr, individual_pl): 676/676, wybrany ten sam punkt
    wag, 2018-01-05..2026-06-26 (443 tygodnie, kalendarz ograniczony dywidendami),
    final_wealth_pre_tax 3 785 934.37, after_tax_terminal_wealth 3 131 083.58, CAGR 16.98%,
    after-tax CAGR 14.39%, max DD 34.11%, after-tax max DD 37.05%, podatki 504 542.60.

## Walk-forward (sesja 12)

98. **Q-022 (RESOLVED) - model**: TRAIN to niezależny, hipotetyczny backtest służący wyłącznie
    do wyboru parametrów; OOS to jedna ciągła ścieżka portfela. Kandydaci treningowi są pełnymi
    runami (`run_prepared`: podatki/koszty, terminal settlement, pre-tax shadow, metryki - np.
    fundacja płaci w treningu setup/admin), ale stan treningowy nigdy nie staje się stanem OOS.
    Z treningu do OOS przechodzą tylko: wybrane wagi i parametry oraz snapshot stanu sygnału
    wybranego kandydata na koniec okna treningowego (z oczekującymi wykonaniami).
99. **Dane raz**: najpierw cały grid (wagi Q-041 x wymiary sygnałowe), unia aktywów wagowo
    poprawnych kandydatów, maksymalne MA / confirmation / delay gridu (warm-up), potem jedno
    `prepare_run(assets=unia, warmup_params=max, auto_start=True)`. Bez `--start` pierwszy
    tydzień = pierwszy wspólny tydzień z warm-upem najbardziej wymagającego punktu gridu (warm-up
    nie wchodzi do celu); `run.start` jest początkiem najwcześniejszej historii treningowej.
100. **Widok treningowy (WF-004/014, META-003)**: `PreparedRun.training_view(train_start,
    train_end, test_start)` fizycznie zawiera wyłącznie tygodnie okna, ich rekordy rynku,
    obserwacje sygnału z `week_key < test_start` i `available_at < test_start` (cała wcześniejsza
    historia zostaje do warm-upu), issues i wiersze znormalizowane sprzed test_start, bez CPI.
    `max_information_week() < test_start` jest zapisane per okno w `walk_forward_manifest.json`
    (`no_future_training_checks_passed`). `training_input_sha256` hashuje treść widoku (bez
    hashy całych plików źródłowych, które obejmują też przyszłość).
101. **Okna (WF-002/003/011/012/015/016, Q-020)**: pierwszy anchor = najwcześniejszy tydzień
    treningowy + `train_years` lat kalendarzowych (29 II -> 28 II); `test_start` = pierwszy
    zachowany piątek >= anchor; `train_end` = zachowany tydzień przed test_start; rolling:
    `train_start` = pierwszy tydzień >= anchor - train_years, anchored: stały start. Kolejne
    anchory co `step_years` (domyślnie test_years; całkowity = lata kalendarzowe, ułamkowy =
    round_half_up(step x 365.2425) dni). Segment OOS i = [test_start_i, tydzień przed
    test_start_{i+1}], ostatni do końca danych (zawsze zachowany, WF-015); `nominal_test_end` =
    ostatni piątek przed anchor + test_years; `actual_test_days` = actual_oos_end - test_start +
    1, `actual_test_years` = dni / 365.2425. Brak pełnego okna treningowego ->
    `InsufficientHistoryError` "insufficient history for requested walk-forward training window"
    (kod 1), okno treningowe nigdy nie jest skracane.
102. **Grid (WF-006..010)**: iloczyn wyłącznie parametrów z `--optimize-params` w kolejności
    wagi (zewnętrzne, grid Q-041), ma, threshold, delay, confirmation, sell_fraction,
    rebalance_band; stabilny `grid_index`. Jedna wartość gridu ustawia parametr wszystkich
    aktywnych aktywów ryzykownych (threshold: off = on = p w procentach CLI; confirmation: off =
    on = N). Domyślne gridy: ma [50], threshold [0.03], delay [1]; confirmation wymaga
    `--confirmation-grid`, rebalance_band wymaga `--band-pp` (każdy kandydat: rebalance=band,
    band_pp=p); `--sell-fraction` przyjmuje listę/zakres dziesiętny tylko w walk-forward z
    sell_fraction w `--optimize-params` (bez gridu - wartości z configu). Bez `weights` wymagane
    jawne `allocation.targets` (ALLOC-001). Grid dla parametru spoza listy -> ConfigError.
    CLI-011: 676 x 3 x 4 x 4 = 32 448 kandydatów na okno.
103. **Selekcja treningowa**: dokładnie logika in-sample optimizera (`evaluate_candidates` +
    `assemble`: cele OPT-007, limit maxDD, objective_unavailable, odrzucenia wag, tie-break Q-041
    rozszerzony o grid_index tylko dla identycznych wag). `--jobs` zrównolegla kandydatów w
    obrębie okna, okna są sekwencyjne; wyniki bajtowo identyczne dla `--jobs 1` i `2`.
104. **Pierwsze okno OOS**: nowy portfel z initial_capital, wybranymi wagami i stanami
    sygnału z treningu (alokacja początkowa: bez Trade, kosztów i turnover; setup fundacji raz);
    oczekujące wykonania z treningu z terminem w OOS są przejmowane (termin w pierwszym tygodniu
    -> krok 1).
105. **Kontynuacja (WF-013, TEST-037)**: po każdym segmencie `OOSContinuationState`
    (PortfolioSnapshot: ledger z rf_base i rezerwami, loty z id i cost basis, next_lot_id, ceny
    jednostkowe; kopia TaxState/FoundationState: realizacje, loss buckets, otwarty rok,
    zobowiązania, stan admin, sumy, zdarzenia; snapshoty trackerów; oczekujący trigger band;
    ostatni tydzień; cele, parametry i rebalancing). Kolejny segment startuje przez
    `engine.EngineStart` (bez alokacji i kosztów) z hookami na przeniesionym stanie; roczny
    podatek / koszt admin może zostać ustalony w kroku 2 pierwszego tygodnia nowego okna. Przy
    stałej selekcji sklejona ścieżka jest dokładnie równa jednemu ciągłemu runowi (NAV, transakcje,
    płatności, rebalancing z przeniesionym triggerem band, zdarzenia podatkowe, terminal,
    shadow, wszystkie metryki) dla none, individual_pl i obu fundacji.
106. **Stan sygnału na granicy (WF-014)**: aktywo nadal aktywne z identycznymi parametrami ->
    żywy tracker kontynuowany (okno SMA, liczniki potwierdzeń, stan, kolejka; jego oczekujące
    wykonania idą jako transakcje w kroku 1, dokładnie jak w ciągłym runie; zgodność ze stanem
    treningowym raportowana w `carried_state_matches_training`); zmienione parametry lub nowe
    aktywo -> snapshot wybranego kandydata TRAIN (instalowany na test_start; wykonania z
    terminem przed nim ustawiają tylko stan), stare oczekujące wykonania anulowane, nowe
    przejęte; aktywo nieaktywne -> anulowanie jego kolejki. Snapshot (`SignalTrackerSnapshot`)
    jest niemutowalny, memo rekonstrukcji zwraca głębokie kopie.
107. **Rebalance na granicy**: wymuszany przy zmianie wag, zbioru aktywnych aktywów,
    trybu/pasma rebalancingu, zmianie stanu (lub podziału RISK_OFF) podmienionego trackera albo
    osieroconej pozycji; inaczej granica bez transakcji. Kolejność pierwszego tygodnia: stany
    sygnału -> krok 1 -> krok 2 (amounts_due) -> krok 3 `walk_forward_rebalance` -> krok 4 zwroty
    -> krok 5 -> krok 6. Podział docelowy wg alokacji początkowej dla stanu efektywnego (RISK_ON
    100% aktywo; RISK_OFF 1 - sell_fraction / sell_fraction); koszty, slippage, cost basis,
    realizacje, późniejsze podatki; trade_count/turnover; należności tego tygodnia w tym samym
    planie (TAX-007, cele z NAV netto). Oczekujący trigger band przenoszony tylko bez wymuszonego
    rebalance, inaczej anulowany (`cancelled_superseded_by_walk_forward_rebalance`). Aktywo
    wychodzące: sprzedaż z realizacją, jego rezerwa RF włączona (brak osieroconych rezerw).
108. **Shadow i terminal**: ciągły pre-tax shadow (stawki 0, koszty, setup raz, admin, koszt
    końcowy fundacji) z własnym stanem, nigdy resetowany; terminal settlement wyłącznie po
    ostatnim tygodniu OOS (individual_pl raz, fundacja raz, none brak) - na granicach
    wewnętrznych brak terminal_liquidation, foundation_distribution_liquidation, terminalnego
    CGT i podatku od dystrybucji.
109. **Wyjścia**: standardowe artefakty sklejonej ścieżki OOS (summary.csv, weekly_portfolio.csv
    z celami obowiązującymi w danym tygodniu, trades, tax_events, payments, rf_transfers,
    rebalance_events, signals, realizations, terminal_settlement, validation_report,
    config_resolved.yaml, data_manifest.json, weekly_normalized.csv) + `walk_forward_results.csv`
    (REP-016: wiersz na okno OOS: daty, długości, wybrany grid_index, wagi, MA, threshold,
    delay, confirmation, sell_fraction, rebalancing, cel treningowy, NAV/zwrot/transakcje/
    turnover/podatki/koszty OOS, rebalance na granicy), `training_grid_results.csv` (pełne
    gridy z window_id), `walk_forward_boundary_events.csv` i `walk_forward_manifest.json`.
    summary.csv: tylko sklejony OOS (growth_base_nav = kapitał początkowy, inception = piątek
    przed pierwszym tygodniem OOS) + kolumny optimization_mode, walk_forward_window,
    train_years, test_years, step_years, oos_windows, first_oos_week, last_oos_week; cele i
    parametry sygnałów w summary tylko, gdy wszystkie okna miały te same.
110. **WF-005 (SHOULD)**: każdy wiersz `training_grid_results.csv` ma `eligible_rank`,
    `objective_gap_to_selected`, `selected_neighbour`; manifest per okno: top-5 eligible z luką
    celu i sąsiedzi wyboru w gridzie (jeden krok na jednej osi) z luką celu.
111. **Wyniki**: dokładne CLI-011 na danych staged -> kod 1 "insufficient history for requested
    walk-forward training window: train_years=15 needs history from 2012-10-05 to 2027-10-05,
    the common data ends 2026-08-28" (Q-020; kalendarz ograniczony BTC). Syntetyczne 26.5 roku
    (1990-01-05..2016-06-10) z flagami CLI-011 i zredukowanym gridem wag (btc 0/10, gold 0/10,
    192 kandydatów na okno, --jobs 2, ~30 s): OOS 2006-04-28..2011-04-22 (1821 dni),
    2011-04-29..2016-04-22 (1821 dni), 2016-04-29..2016-06-10 (43 dni, częściowe), kod 0.
    Scenariusz S09 w AB_SCENARIOS pozostaje bez zmian (brak sztucznych wyników).

## Domknięcie MUST przed freeze (sesja 13)

112. **Q-013 (RESOLVED) - warm-up**: dla `signal.initial_state=reconstruct_history` historia
    przed pierwszym tygodniem krótsza niż `ma + max(confirm_off, confirm_on) + delay` ->
    `WarmupError` (ERR-003) z aktywem, liczbą dostępnych i wymaganych obserwacji, pierwszym
    tygodniem runu i - dla niekanonicznego źródła - jego ostrzeżeniem proweniencji (np. Q-002).
    Wymagań nie skracamy, jawnego `--start` nie przesuwamy, brak flagi `--allow-short-warmup`.
    Jedyny fallback to jawne `signal.initial_state=RISK_ON` (np. w pliku config): run startuje,
    `validation_report.csv` ma warning `warmup_short`, `summary.csv` ma
    `signal_initial_state` i `warmup_fallback_assets`, `data_manifest.json` ma
    `signal_initial_state` i `initial_state_fallback`. CLI-001 na danych staged kończy się
    "ERR-003 insufficient warm-up for gold: available 51 < required 55 weekly observations
    before the first run week 1971-01-01 ... gold source is non-canonical: ... Q-002" (kod 1);
    ta sama komenda na fixture z historią złota od 1960 działa end-to-end.
113. **Q-025 (RESOLVED) - start bez `--start`**: każda komenda bez `run.start` zaczyna od
    najwcześniejszego tygodnia wspólnego zakresu zwrotów, przed którym każde aktywne aktywo ma
    pełny warm-up (`first_warmup_week`; dotąd tylko scany/optimizer), raportowanego jako
    `effective_first_week` i `first_week_rule` (manifest). Przykład: stocks 0.4 / gold 0.4 / rf
    0.2 bez `--start` -> 1971-01-29 (pierwszy tydzień z 55 obserwacjami złota), nie ERR-003 w
    pierwszym wspólnym tygodniu. Wspólna luka kalendarza (żadne źródło nie ma tygodnia, np.
    1933-03-10) jest raportowana i pomijana bez forward-fill (Q-012), nie jest
    `missing.return_policy=error`. Dla runów, w których warm-up był spełniony na początku
    zakresu wspólnego, start jest identyczny jak wcześniej (regresja bez zmian).
114. **TEST-023 / Q-011 / Q-038**: historia stock signal zaczyna się w piątek 1920-01-02 (Q-011;
    brak założenia historii sprzed 1920). Komenda `signals` (oficjalny interfejs signal-only,
    inne znaczenie niż `--rebalance signal-only`, Q-038) analizuje tę historię bez portfela i
    bez serii zwrotów. Pełny run z aktywnym sleeve'em stocks i jawnym `--start` wcześniejszym niż
    pierwszy tydzień używanego `stocks_return_file` kończy się błędem "requested start ...
    precedes the available stocks return history (first available return week 1926-07-02) ...
    provide an alternative stocks_return_file ... (TEST-023)" (kod 1, nic nie jest zapisywane)
    zamiast cichego przesunięcia startu na 1926-07-02; z alternatywnym plikiem zwrotów (format
    Fama/French) pokrywającym wcześniejszy okres run działa od żądanego tygodnia. Pozostałe
    różnice zakresów (koniec danych, dywidendy, BTC, złoto) zachowują obcięcie NORM-011.
115. **DATA-008 / Q-025 - `--data-file`**: klucze `stocks_price`, `stocks_return`, `gold`, `btc`,
    `dividend`, `cpi` (opcjonalnie z sufiksem `_file`, `config.normalize_data_key`); nieznany
    klucz, to samo źródło dwa razy w jednej komendzie (także z bezpośrednią flagą
    `--<klucz>-file`) albo nieznany klucz w `data.overrides` pliku config -> ConfigError (kod 2)
    przed jakimkolwiek odczytem danych, bez "last wins". Override trafia do
    `config_resolved.yaml` (`data.overrides`) i `data_manifest.json` (`data_overrides`:
    config_key, path, sha256, oraz `sources`).
116. **Q-024 / CLI-007**: dosłowny przykład bez wag kończy się ALLOC-001 (bez ukrytej
    alokacji); wariant z `--weights stocks=1.0` i własnym plikiem SPX działa, a manifest
    pokazuje ścieżkę i SHA-256 pliku. **CLI-009**: dokładna komenda S07 jest PASS jako
    mechanika komendy; proxy złota/akcji pozostają osobnymi DATA_BLOCKER (Q-002/Q-004,
    SEM-003/SEM-001) i są raportowane jako ostrzeżenia proweniencji.
117. **TEST-020**: dwa identyczne runy CLI (config, as_of_date, SHA-256, polityki źródeł, kod)
    dla none, individual_pl i family_foundation_19 dają bajtowo identyczne pliki wyników
    (summary, weekly_portfolio, trades, tax_events, payments, rf_transfers, rebalance_events,
    signals, realizations, dividend_reinvestments, config_resolved, weekly_normalized,
    terminal_settlement, tax/foundation_state, validation_report); `data_manifest.json` różni
    się tylko `run_timestamp`. Powtórzony walk-forward (jobs=1) jest bajtowo identyczny poza
    timestampami manifestów.
118. **ARCH-005 / ARCH-009 (SHOULD)**: odpowiedzialności `src/portfolio.py` ze specyfikacji
    są rozdzielone na ledger, rf, costs, cost_basis, prymitywy WorkingPortfolio silnika,
    rebalancing i sell_to_pay (testy własne + test architektury
    `test_portfolio_accounting_layers`); reporting.py i manifest.py zapisują wyniki już
    policzone, żadna warstwa obliczeniowa od nich nie zależy
    (`test_reporting_is_a_separate_layer`).
119. **Odroczone SHOULD (bez wpływu na MUST)**: DIV-009 `spread_annual_dps` (od sesji 15 dane
    trailing DPS istnieją, tryb runtime pozostaje jawnie odroczony; `use_supplied_dividend_return`
    działa, punkt 133), MET-022/023 rolling metrics,
    REP-011 tabela konsolowa. MUST nie-PASS: FND-002 (Q-047), FND-005 (Q-037) oraz 5 wierszy
    DATA_BLOCKER (SEM-001, SEM-003, SCHEMA-005, SEM-007, TEST-038).

## Fundacje: internal trading tax i distribution_schedule (sesja 14)

120. **Q-047 (RESOLVED) - internal trading tax > 0**: osobna warstwa od podatku od dywidend,
    RF i dystrybucji. Rok Friday-key Y: `annual_internal_realized` = suma realized_gain
    wszystkich sprzedaży stocks/gold/BTC fundacji w Y (sygnały, rebalancing, sell_to_pay,
    rebalance walk-forward, finansowanie dystrybucji, likwidacja terminalna), netting w roku,
    podstawa max(0, ...), podatek = podstawa x stawka; bez loss buckets, carry-forward i daniny
    (strata roku przepada). Ustalenie w kroku 2 pierwszego zachowanego tygodnia Y+1 (TaxEvent
    `foundation_internal_trading_tax`, tax, annual, step 2 - także z kwotą 0 dla audytu),
    płatność w kroku 3 (TAX-007 albo TAX-006); sprzedaże finansujące podatek są realizacjami
    Y+1. Przy domyślnej stawce 0 nie powstają żadne zdarzenia (wyniki bajtowo identyczne).
    Przykład (TEST-015, stawka 0.10): 2000 = jedna sprzedaż sygnałowa 672 000 x 0.9985 -
    336 000 = 334 992 -> podatek 33 499.20 ustalony i zapłacony 2001-01-05; rok straty
    -168 252 + 35 730 = -132 522 -> podatek 0, 2001 opodatkowany w całości.
121. **Rok finalny (tax_event=terminal)**: likwidacja -> realizacje terminalne do roku
    finalnego -> zamknięcie roku internal tax (event terminal, phase terminal, step None) ->
    konsolidacja RF -> zapłata internal tax -> koszt admin roku finalnego -> distributed_amount
    -> distribution tax -> after_tax_terminal_wealth. `terminal_foundation_tax` = internal tax
    roku finalnego + distribution tax; osobna kolumna `terminal_internal_trading_tax`.
    Przykład (fixture roczny): pre_terminal_nav 1 768 220.65, koszty likwidacji 2 652.33,
    realizacje roku 2002 549 967.63 -> internal tax 54 996.76, admin 1 972.60, distributed
    1 708 598.95, distribution tax 15% 256 289.84, after-tax 1 452 309.11.
122. **Q-037 (RESOLVED) - distribution_schedule**: wymaga `tax.foundation.distribution_file`
    (`--distribution-file`); brak klucza, pliku albo błędny wiersz -> ConfigError (bez fallbacku
    do terminal). CSV `date,amount` i/lub `date,percent_nav`, w każdym wierszu dokładnie jedno:
    amount > 0 PLN (brutto), 0 < percent_nav <= 1 (ułamek dziesiętny, nie procent CLI).
    Harmonogram jest egzogenicznym planem (nie dane rynkowe, nie uczestniczy w przecięciu
    kalendarza): data -> piątek tego samego tygodnia (nominal_week); tydzień usunięty z
    kalendarza -> następny zachowany tydzień (actual_week); wiersze przed pierwszym / po
    ostatnim tygodniu runu ignorowane z ostrzeżeniem `distribution_row_ignored`; wiele wierszy
    w tygodniu wg (scheduled_date, indeks wiersza). Manifest: path, SHA-256, liczba wierszy,
    min/max data, liczba wierszy amount i percent_nav.
123. **Semantyka brutto (Q-037)**: wiersz D to wypłata brutto zdejmowana z NAV fundacji;
    podatek T jest potrącany z D, beneficjent dostaje D - T, odpływ NAV = D (nigdy D + T).
    percent_nav: D = p x NAV_after_signal kroku 2 (po transakcjach sygnałowych, przed
    amounts_due, rebalancingiem i zwrotami tygodnia). Krok 2: jedna pozycja finansowania
    `foundation_distribution_gross` = D na wiersz (razem z kosztem admin w tym samym tygodniu;
    księga widzi D niezależnie od stawki, więc ścieżka NAV fundacji 15% i 19% jest bajtowo
    identyczna); krok 3: TAX-007 przy rebalancingu (NAV_net = NAV_after_signal - suma
    należności), inaczej TAX-006 z powodem sprzedaży `foundation_distribution_liquidation`
    (koszty i slippage, REB-011). Po sfinansowaniu każda opłacona część jest zapisana jako
    Payment `foundation_distribution_tax` (T) + `foundation_distribution_net` (D - T) -
    `WorkingPortfolio.split_payment` dzieli tylko rekord, księga nie jest obciążana drugi raz.
    TaxEvent tylko dla podatku (settlement `scheduled`, phase weekly, step 2); wypłata netto nie
    jest podatkiem, kosztem ani transakcją; audyt w `distributions.csv` (scheduled_date,
    nominal_week, actual_week, paid_week, kind, value, nav_base, gross, tax_base, tax, net,
    basis). weekly_portfolio.csv: `gross_distributions_paid` = `net_distributions_paid` +
    `distribution_tax_paid` (payments = koszty + podatki roczne + wypłaty netto).
124. **Podstawa dystrybucji**: distributed_amount -> każda wypłata D; gain_only -> kumulatywna
    baza `distribution_capital_basis_remaining` (start = initial_capital_pln przed setup):
    capital_return = min(D, basis), podstawa = D - capital_return, basis -= capital_return -
    skumulowane wypłaty <= kapitał początkowy dają 0, podatek tylko od skumulowanej nadwyżki,
    niezależnie od podziału i kolejności (jedna wypłata 1.2 mln = 0.6 + 0.6 mln -> podstawa
    0.2 mln przy kapitale 1 mln).
125. **Koniec w trybie schedule i metryki (świadoma semantyka Q-037)**: brak pełnej
    likwidacji `foundation_distribution_liquidation` i brak terminalnego podatku od
    dystrybucji tylko dlatego, że backtest się kończy; po ostatnim tygodniu (lub ostatnim
    tygodniu OOS) pobierany jest wyłącznie koszt admin roku finalnego (FND-011, waterfall),
    portfel pozostaje zainwestowany. `after_tax_terminal_wealth` = pozostały NAV fundacji +
    skumulowane wypłaty **netto**; pre-tax shadow wykonuje ten sam harmonogram brutto ze
    stawkami 0: `final_wealth_pre_tax` = pozostały NAV shadow + skumulowane wypłaty **brutto**
    (koszty setup/admin pozostają). Tygodniowa ścieżka NAV maleje o każdą wypłatę brutto, więc
    drawdown może zawierać wpływ rzeczywistych wypłat; terminal nie tworzy sztucznego
    drawdownu; otrzymane wypłaty dodawane są dopiero do terminal wealth / licznika CAGR.
    summary.csv: `foundation_gross_distributions_paid`, `foundation_net_distributions_paid`,
    `foundation_distribution_tax_paid`, `distribution_capital_basis_remaining`;
    `distributed_amount` puste i podatki terminalne 0. W trybie terminal nowe pola mają 0 /
    puste, znaczenie istniejących pól bez zmian.
126. **Ograniczenie (zapisane w data_manifest.json `foundation_limitations`)**:
    `distribution_schedule` razem z `internal_trading_tax_rate > 0` nie jest zdefiniowane przez
    adjudykację (brak semantyki roku finalnego bez likwidacji przy wymuszonych sprzedażach) ->
    ConfigError "non-zero foundation internal trading tax with distribution_schedule is not
    defined by clean-room specification adjudication". FND-002 i FND-005 są PASS: niezerowy
    internal tax jest w pełni obsługiwany w trybie terminal, oba tryby FND-005 przy stawce 0.
127. **Walk-forward**: FoundationState (internal_tax_by_year, closed_internal_tax_years,
    sumy wypłat, baza gain_only, distribution_events) przechodzi przez granice okien;
    harmonogram jest mapowany raz na sklejony kalendarz OOS, więc każdy wiersz jest wypłacany
    dokładnie raz (wiersz tylko w historii treningowej jest ignorowany przez ścieżkę OOS z
    ostrzeżeniem); kandydaci treningowi stosują wiersze swojego zakresu do własnej hipotetycznej
    oceny. Przy stałej selekcji sklejona ścieżka jest dokładnie równa jednemu ciągłemu runowi
    (internal tax ustalany w pierwszym tygodniu nowego okna; wypłaty i baza przenoszone).
128. **Zgodność wsteczna**: domyślna konfiguracja fundacji (stawka 0, tax_event=terminal) daje
    bajtowo identyczne wyniki poza nowymi zerowymi/pustymi kolumnami i kluczami (24 komendy
    regresji: 241 plików bajtowo identycznych, pozostałe różnią się tylko nowymi polami 0/puste).
    Tydzień, w którym harmonogram wypłaci cały NAV, kończy ścieżkę z NAV 0 (zwrot tygodnia 0).

## Dane kanoniczne akcji i dywidend (sesja 15)

129. **Handoff i staging**: dwa finalne pliki z handoffu skopiowano bez zmian do
    `input/data/` pod nazwami domyślnymi specyfikacji po sprawdzeniu SHA-256 i zawartości:
    `US_STOCK_PRICE_WEEKLY_1885_2026.csv` (af1da27aa7d8dc689cb5d2f57f4a9c37e70f95fab18514a5f3087b98078987bd)
    i `SPX_dividend_return_weekly_1970_2026.csv`
    (a62191993c42a5692072323d34947d2095b0e5ca162ca63c23ed91ebba7e8cf5). Obok nich sidecary
    `<plik>.provenance.json` (metadane budowy: SHA-256 surowych plików, metoda, rebase, segmenty,
    estymata 2026, SHA-256 pliku końcowego). `input/source_manifest.json` ma pole
    `data_status` (specification/canonical/provenance/proxy/supplemental) dla każdego pliku;
    `tools/verify_inputs.py` sprawdza hashe, zgodność sidecarów z plikami i wypisuje, które
    domyślne nazwy DATA-* istnieją. Surowe pliki dostawców (stkdatd.zip, ie_data.xls, eksport
    TradingView) nie są w repozytorium i nie zostały dostarczone do sesji: ich SHA-256 są
    deklaracją handoffu; w repo nie ma polityki licencyjnej danych, więc niczego nie
    redystrybuowano.
130. **Sygnał akcji (SEM-001, Q-004 RESOLVED)**: Schwert price-only (capital gain return,
    `index_t = index_(t-1) * (1 + cg_t)`, ostatnia obserwacja tygodnia Mon-Sun -> piątek)
    1920-01-02..1927-12-30 (418) + TradingView SPX 1W Close 1928-01-06..2026-09-25 (5151)
    przemnożony przez 23.2423477355492638731596828992 = 410.4598610098 / 17.66 (anchor
    1927-12-30, który zostaje w pliku jako Schwert). Loader czyta segmenty z kolumny `source`
    (verified), `rebase_factor` segmentu SPX i blok `splice` z sidecara; `canonical=true`, bez
    ostrzeżeń. `source_date` (Schwert: data ostatniej sesji, zwykle sobota; SPX: timestamp
    tygodniowego bara, zwykle poniedziałek) jest wyłącznie metadaną obserwacji
    (`weekly_normalized.csv`), dostępność informacji pozostaje piątkiem week_key (NORM-019);
    `source_date` spoza tygodnia wiersza jest błędem (NORM-013). Jedyna luka 1933-03-10 nie
    jest wypełniana (Q-012). Nazwa pliku z 1885 nie rozszerza historii (start 1920-01-02, Q-011).
131. **Weryfikacja sygnału akcji** (`work/tools/verify_canonical_data.py` ->
    `work/audit/canonical_data_checks.csv`, niezależnie od `src`): segment Schwert identyczny
    (stringi cen) z plikiem supplemental Schwert; implikowane surowe zamknięcia SPX leżą na
    siatce 0.01 (szum float TradingView <= 0.001), 1928-01-06 = 17.66, 1928-01-13 = 17.58;
    zwroty SPX 1928-1962 zgodne z Schwert w tym samym tygodniu (korelacja 0.92 vs 0.12/0.03 dla
    przesunięć ±1); po 1962 te same zamknięcia SPX co w staged proxy (stały iloraz). Korelacja
    FF z nowym indeksem (NORM-018) wynosi 0.957 (0.99 od 1963): przed 1953 FF/CRSP mają tygodnie
    sobotnie, a historia SPX TradingView zamknięcia piątkowe, i S&P to inny indeks niż CRSP VW;
    wyrównanie tygodni jest poprawne (przesunięcia < 0.1). Test NORM-018 sprawdza te progi.
132. **Dywidendy (SCHEMA-005, SEM-007, TEST-038, Q-008 RESOLVED)**: dokładnie kolumny
    SCHEMA-005; 2960 piątków 1970-01-02..2026-09-18, kroki 7 dni, bez luk i duplikatów;
    `year == date.year`; wszystkie pola > 0; maksymalny błąd względny `dividend_return` vs
    `dividend_points / spx_close_prev` = 0.0; `spx_close`/`spx_close_prev` równe kanonicznemu
    sygnałowi akcji w tygodniu / poprzednim piątku; status actual 1970-01-02..2025-12-26 (2922)
    i estimate 2026-01-02..2026-09-18 (38). Budowa: Shiller `ie_data.xls` (Date, P, D),
    `trailing_dps_points = D * 23.2423477355492638731596828992` (tylko zmiana jednostki),
    `annual_yield_pct = D / P * 100`, `dividend_points = trailing_dps_points / liczba piątków
    roku` (52/53), `dividend_return = dividend_points / spx_close_prev`. Estymata 2026 (nigdy
    `actual`): Q3 2026 estimate = 21.13 index points; TTM Sep 2026 = TTM Jun 2026 (81.7032) -
    Q3 2025 actual (19.81) + Q3 2026 estimate (21.13) = 83.0232; lipiec/sierpień interpolacja
    liniowa (82.1432, 82.5832). Loader canonical dodatkowo: ujemne pola -> błąd walidacji
    `dividend_value`; bloki statusów w manifeście (`status_blocks`), blok actual po bloku
    estimate -> ostrzeżenie `dividend_status_order`.
133. **DIV-009 (SHOULD)**: plik kanoniczny realizuje ekonomicznie `spread_annual_dps`
    (zweryfikowane: `dividend_points = trailing_dps_points / piątki roku`, błąd 0.0), ale tryb
    runtime `tax.dividend_approx_method=spread_annual_dps` pozostaje jawnie odroczony; runtime
    `use_supplied_dividend_return` czyta gotowy `dividend_return`.
134. **Rozdzielczość domyślna**: istniejący plik domyślny DATA-001 / DATA-007 jest używany
    bezpośrednio (`resolved_via_alias=false`, adapter `week_end` / `canonical`); alias profilu
    `cleanroom_data.yaml` pozostaje tylko fallbackiem dla data.dir bez tych plików (złoto i BTC
    nadal przez alias, złoto `tvc_oanda_proxy`, `canonical=false`, ostrzeżenie Q-002). Staged
    proxy akcji/dywidend można przypiąć jawnie: `--config work/config/staged_proxy_data.yaml`
    (overrides + adapter `shiller_proxy` + deklarowane segmenty). Sidecar provenance
    (dopasowany po SHA-256 pliku) trafia do `data_manifest.json` jako `extra.build_provenance`
    (SHA-256 surowych wejść, podsumowanie, splice/rebase, estymata 2026) bez wpływu na wartości;
    sidecar opisujący inną zawartość jest ignorowany z ostrzeżeniem.
135. **Testy: mechanika vs dane kanoniczne**: testy mechaniki z wartościami zamrożonymi na
    danych staged przypinają staged proxy jawnie (`fixtures.builders.staged_proxy_layer` /
    `staged_proxy_overrides`) i zachowują dotychczasowe wartości (regresja Q-033, mechanika
    supersetu kalendarza tax-compare na pliku dywidend kończącym się 2026-06-26). Testy
    integracji danych przełączono na pliki kanoniczne: TEST-038 na prawdziwym pliku
    (`test_dividend_input_file_canonical`), SEM-001 w manifeście i wokół splice
    (1927-12-30 Schwert, 1928-01-06/13 SPX), manifest canonical/proxy, SEM-011/TEST-051 na
    prawdziwym wierszu 2026-09-25 (Q-010 RESOLVED), granica statusów 2025-12-26 / 2026-01-02 z
    trzema politykami DIV-011, CLI-005 (koniec 2026-07-31 zamiast 2026-06-26), statusy
    dywidend w tax_events (actual do 2025, estimate w 2026), CLI-009 (brak ostrzeżenia
    provenance akcji, ostrzeżenie złota pozostaje).
136. **Skutki liczbowe (oczekiwane, nie regresja silnika)**: wyniki zależne od dywidend
    (profile podatkowe ze smoothed_weekly) i od sygnału akcji przed 1963 (Schwert -> SPX
    1928-1962: S02/S03 od 1971 z historią sygnału, runy od 1926) zmieniają się; sygnał akcji od
    1963 ma te same stany (ta sama seria SPX z innym stałym mnożnikiem, stosunek ceny do SMA bez
    zmian), więc w runach profilu none od 2015/2018 summary/weekly_portfolio/trades są bajtowo
    identyczne, a signals.csv różni się tylko poziomem ceny/SMA/pasm. S05/S06/CLI-005 nie są już
    obcinane do 2026-06-26 (dywidendy do 2026-09-18; koniec = `--end` 2026-07-31); estymata
    dywidend dotyczy tylko tygodni 2026. Zestaw 24 komend (385 plików) z jawnie przypiętymi
    staged proxy (`--config work/config/staged_proxy_data.yaml`) odtwarza bajtowo wszystkie pliki
    wynikowe sprzed sesji (summary, weekly_portfolio, trades, tax_events, signals, grid_results,
    ...); różnice wyłącznie w metadanych (config_resolved, data_manifest/hashe konfiguracji, brak
    ostrzeżeń `default_file_alias` przy jawnym przypięciu, nowy klucz `status_blocks`), czyli
    silnik bez zmian. Na danych kanonicznych: tożsamość NAV = suma składników (błąd 0), ciągłość
    nav_start = poprzedni nav_end, `total_tax_paid` = suma tax_events (<= 1.3e-16) we wszystkich
    20 runach z portfelem; S05 jobs=1 vs jobs=4 i dwa runy S07 identyczne (różni się tylko
    `run_name`).
137. **Pozostały DATA_BLOCKER**: wyłącznie SEM-003 / Q-002 (LBMA Gold PM); staged złoto
    pozostaje proxy `tvc_oanda_proxy`. `freeze_v2.py` nie był uruchamiany.
138. **Poprawka raportowania ujawniona przez dane kanoniczne (osobno od danych)**: seria
    dywidend (smoothed_weekly) nie przechodziła kompletności NORM-019 (`apply_completion`) jak
    pozostałe źródła, więc plik sięgający poza `run.end` / `as_of` dawał fałszywe ostrzeżenie
    `range_truncated dividend: end truncated from 2026-09-18 to common 2026-07-31` (staged plik
    kończył się przed końcem runu, więc nie było to widoczne). Teraz tygodnie dywidend po
    min(run.end, as_of) nie należą do zakresu źródła (wiersze po as_of raportowane jako
    `incomplete_week_dropped`, jak dla innych źródeł). Bez wpływu liczbowego (tygodnie po końcu
    kalendarza nigdy nie były używane); test regresyjny
    `test_dividend_range_follows_norm019_completion` (pada bez poprawki).

