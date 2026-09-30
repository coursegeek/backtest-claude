# IMPLEMENTATION_NOTES — Backtest V2 (clean-room)

Stan: **wszystkie cztery profile podatkowe (none, individual_pl, family_foundation_15/19 z
tax_event=terminal) w centralnym pipeline + terminal settlement + pre-tax shadow run + metryki +
summary.csv** (sesja 8) **oraz komenda `tax-compare`** (sesja 9). Zaimplementowane: CLI i API
importu, konfiguracja, modele, kalendarz, dostępność informacji, loadery z normalizacją
kanoniczną, walidacja (semantyka luk Q-012), pipeline sygnałów, ledger, koszty transakcyjne, cost
basis (lots), centralny tygodniowy engine PORT-011, rebalancing
`signal-only/weekly/monthly/quarterly/annually(yearly)/band`, sell_to_pay (TAX-006), finansowanie
należności przy rebalancingu (TAX-007), moduł podatkowy `individual_pl` (`src/tax.py`), moduł
fundacji (`src/foundation.py`), terminal settlement (`src/settlement.py`), pre-tax shadow run
(Q-015), moduł metryk (`src/metrics.py`), `summary.csv` i orkiestrator `tax-compare`
(`src/tax_compare.py`).
Nie ma jeszcze: `tax.foundation.tax_event=distribution_schedule` (Q-037, jawny błąd),
niezerowego internal trading tax fundacji (Q-047, jawny błąd), rolling_metrics.csv
(MET-022/023, SHOULD), scanów (delay/threshold/rebalance), optimize, walk-forward. Te tryby
kończą się kodem 3 z jawnym komunikatem (bez częściowych wyników).

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
python work/tools/check_audit_consistency.py --allow-pass
```

## Układ kodu

`work/backtest.py` (CLI + fasada importu) → `src/cli.py` → `src/app.py` (komendy).
Warstwy: `models`, `errors`, `config`, `calendar`, `availability`, `data_loader`, `validation`,
`signals`, `confirmation`, `scheduling`, `signal_analysis`, `allocation`, `rf`, `costs`,
`cost_basis`, `ledger`, `engine`, `rebalancing`, `sell_to_pay`, `tax`, `foundation`, `settlement`,
`metrics`, `manifest`, `reporting`, `tax_compare` (orkiestrator nad `app.prepare_run` / `app.run_prepared`). `src` jest pakietem importowanym jako `src.*` (Q-044).

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
