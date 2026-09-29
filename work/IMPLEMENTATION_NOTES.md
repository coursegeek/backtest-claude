# IMPLEMENTATION_NOTES — Backtest V2 (clean-room)

Stan: **foundation/core + core portfolio engine + rebalancing/sell_to_pay + podatki
individual_pl** (sesja 5). Zaimplementowane: CLI i API importu, konfiguracja, modele,
kalendarz, dostępność informacji, loadery z normalizacją kanoniczną, walidacja (semantyka luk
Q-012), pipeline sygnałów, ledger, koszty transakcyjne, cost basis (lots), centralny tygodniowy
engine PORT-011, rebalancing `signal-only/weekly/monthly/quarterly/annually(yearly)/band`,
sell_to_pay (TAX-006), finansowanie należności przy rebalancingu (TAX-007) oraz moduł podatkowy
`individual_pl` (`src/tax.py`: roczny CG z koszykami strat, danina solidarnościowa, podatek od
dywidend z reinwestycją netto, podatek od RF); komenda `run` dla `tax.profile` none i
individual_pl we wszystkich trybach rebalancingu.
Nie ma jeszcze: terminal settlement (ostatni rok podatkowy runu pozostaje otwarty i nie jest
rozliczany), profili fundacji i ich kosztów, pre-tax shadow run (Q-015 - silnik gotowy),
metryk i `summary.csv`, scanów, optimize, tax-compare, walk-forward. Te tryby rozwiązują i
walidują config, po czym kończą się kodem 3 z jawnym komunikatem (bez częściowych wyników).

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
python work/tools/check_audit_consistency.py --allow-pass
```

## Układ kodu

`work/backtest.py` (CLI + fasada importu) → `src/cli.py` → `src/app.py` (komendy).
Warstwy: `models`, `errors`, `config`, `calendar`, `availability`, `data_loader`, `validation`,
`signals`, `confirmation`, `scheduling`, `signal_analysis`, `allocation`, `rf`, `costs`,
`cost_basis`, `ledger`, `engine`, `rebalancing`, `sell_to_pay`, `tax`, `manifest`, `reporting`. `src` jest pakietem importowanym jako `src.*` (Q-044).

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
    Q-030 pozostaje formalnie OPEN; zaimplementowano formułę z polecenia sesji 5 (pkt 35).
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
    Ostatni (otwarty) rok runu nie jest rozliczany - wymaga terminal settlement.
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
