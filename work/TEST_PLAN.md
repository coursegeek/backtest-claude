# TEST_PLAN — Backtest V2 (clean-room)

Testy są wyprowadzane wyłącznie ze specyfikacji 3.1 i z ręcznych obliczeń zapisanych w samych
testach. Żadne wartości oczekiwane nie pochodzą z V1. Każdy test ma w docstringu listę
requirement ID; mapa wymaganie → test jest w `work/compliance_matrix.csv` (kolumna `test`).

## 1. Narzędzia i układ

* Framework: `pytest` (zależność deweloperska; w środowisku do zainstalowania). Uruchomienie:
  `python -m pytest work/tests`. Markery: `spec`, `unit`, `property`, `integration`, `realdata`, `slow`.
* `work/tests/conftest.py` dodaje `work/` do `sys.path` (import `src.*`, nigdy `work/src` — Q-044).
* Katalogi:
  * `tests/spec/` — TEST-001..TEST-054 (1 funkcja = 1 wiersz TEST, nazwy z tabeli niżej),
  * `tests/unit/` — testy modułów (`test_<moduł>.py`),
  * `tests/property/` — niezmienniki i właściwości,
  * `tests/integration/` — CLI, pliki wyjściowe, dane staged (`realdata`),
  * `tests/fixtures/` — budowniczowie syntetycznych serii w kodzie (`builders.py`) + małe CSV;
    dane z `input/` są tylko czytane.
* Fixture'y syntetyczne mają jawne ceny/zwroty i ręcznie policzone oczekiwania w komentarzu.
  Tolerancje: 1e-12 dla arytmetyki, względna 1e-10 dla tożsamości NAV (Q-049).

## 2. Testy specyfikacji TEST-001..TEST-054

| TEST | Weryfikuje | Planowany test | Fixture (skrót) | Wymagania | Pytania |
|---|---|---|---|---|---|
| TEST-001 | SMA50 = ręczna średnia 50 tygodni | tests/spec/test_signals_spec.py::test_sma50_manual | ceny `p_t=100+t`, t=0..59; SMA brak dla t<49; SMA(49)=124.5, SMA(59)=134.5; porównanie z pętlą ręczną | SIG-001 | Q-028 |
| TEST-002 | sygnał z T nie zmienia zwrotu T | tests/spec/test_no_lookahead_spec.py::test_signal_T_not_affect_return_T | 2 aktywa, znane zwroty; exit potwierdzony na końcu T; wariant bez sygnału ma identyczny NAV do końca T, różnica dopiero od T+1 | META-003, PORT-012 | — |
| TEST-003 | confirmation N + delay=1 | tests/spec/test_signals_spec.py::test_confirmation_delay_pipeline | N=3: warunek w tyg. 10,11,12 → potwierdzenie 12, wykonanie na początku 13; delay=2 → 14; przerwa w 11 → brak potwierdzenia | SIG-016, DELAY-002, DELAY-004 | — |
| TEST-004 | histereza ±3% | tests/spec/test_signals_spec.py::test_hysteresis_3pct | SMA stałe 100; ceny 97..103 → stan bez zmian; 96.9 liczy exit; 103.1 liczy entry; równość z pasmem = wewnątrz | SIG-006 | — |
| TEST-005 | sell_fraction=0.5 tylko przy ON→OFF | tests/spec/test_risk_off_spec.py::test_sell_fraction_half_only_on_transition | single-asset stocks 1 000 000 w tygodniu wykonania → sprzedaż dokładnie 500 000; kolejne tygodnie RISK_OFF bez transakcji | RISK-001, RISK-007 | — |
| TEST-006 | wpływy do własnej rezerwy, inne holdings bez zmian | tests/spec/test_risk_off_spec.py::test_proceeds_to_own_reserve | stocks/gold/btc/rf_base; exit stocks → `rf_reserve_stocks` += wpływy netto; pozostałe pozycje identyczne co do bitu | RISK-002, RISK-003 | — |
| TEST-007 | Mkt-RF+RF z procentów na ułamek | tests/spec/test_returns_spec.py::test_ff_percent_to_decimal | wiersz `19260702, 1.58, -0.61, -0.90, 0.06` → R_stock=0.0164, R_rf=0.0006 | PORT-001, PORT-002 | — |
| TEST-008 | zwrot złota = iloraz cen | tests/spec/test_returns_spec.py::test_gold_return_price_ratio | ceny 100, 110, 99 → 0.10, −0.10; plik bez weekly_return → liczone z ceny | PORT-003, SCHEMA-003 | — |
| TEST-009 | grid odrzuca sumę wag >100% | tests/spec/test_optimizer_spec.py::test_grid_rejects_sum_over_100 | btc 0:60:30, gold 0:60:30, rf 0; kombinacja 60+60 odrzucona ze statusem; stocks=remainder ≥ 0 | OPT-006 | Q-041 |
| TEST-010 | detal: 19% CG tylko od zysku zrealizowanego | tests/spec/test_tax_individual_spec.py::test_cg_only_realized | zakup 100@100; rok 1 cena 150 bez sprzedaży → CG 0; rok 2 sprzedaż połowy → zysk 2 500 → podatek 475 należny w 1. tygodniu roku 3 | IND-001, IND-012 | — |
| TEST-011 | detal: dividend tax 19% | tests/spec/test_tax_individual_spec.py::test_dividend_tax_19 | wartość akcji 1 000 000, d=0.0005 → brutto 500, podatek 95 (smoothed); exact z syntetycznym plikiem cash → ta sama formuła w tygodniu wypłaty | DIV-002, DIV-005 | Q-036 |
| TEST-012 | fundacja: dividend tax 15% | tests/spec/test_tax_foundation_spec.py::test_dividend_tax_15 | jak TEST-011, profil family_foundation_15 i _19 → 75 | DIV-003, FND-001 | — |
| TEST-013 | net return po podatku dywidendowym | tests/spec/test_returns_spec.py::test_stock_net_return_after_dividend_tax | R_total=0.01, d=0.0005, rate=0.19 → 0.009905 | DIV-007 | — |
| TEST-014 | danina 4% ponad 1 mln, wyłączenia | tests/spec/test_tax_individual_spec.py::test_solidarity | CG netto 1 200 000 + external 100 000 → 12 000; dodanie dywidend 50 000 i RF 20 000 nie zmienia daniny; CG 900 000 → 0 | IND-002, IND-005, IND-018 | — |
| TEST-015 | fundacja: internal trading tax = 0 | tests/spec/test_tax_foundation_spec.py::test_internal_trading_tax_zero | fundacja z zyskiem zrealizowanym 100 000 → brak zdarzeń CG, podatek 0 | FND-002 | Q-047 |
| TEST-016 | distribution tax 15% / 19% | tests/spec/test_tax_foundation_spec.py::test_distribution_tax_by_profile | kwota dystrybucji 2 000 000 → 300 000 / 380 000; wariant gain_only | FND-003, FND-004, FND-006 | Q-032 |
| TEST-017 | exact bez pliku cash = błąd; smoothed akceptuje plik | tests/spec/test_dividends_spec.py::test_exact_requires_cash_file_smoothed_accepts_file | exact bez `dividend_cash_file` → DividendModeError; exact z plikiem smoothed → błąd; smoothed z plikiem o schemacie staged → OK + ostrzeżenie estimate | DIV-010, ERR-004 | Q-008, Q-036 |
| TEST-018 | BTC: brak użycia niedzielnego close przed close_date | tests/spec/test_no_lookahead_spec.py::test_btc_sunday_close_availability | wiersze BTC z kluczem piątek K i close K+2; DataView na piątek K nie widzi ceny K; sygnał z K wpływa najwcześniej na tydzień K+7; brak transakcji datowanej na K | NORM-016, SEM-009 | Q-005, Q-007 |
| TEST-019 | koszt transakcji = traded_value*bps/10000 | tests/spec/test_costs_spec.py::test_transaction_cost_reduces_nav | sprzedaż 100 000 przy 10 bps + 5 bps slippage → NAV −150 | REB-003, REB-004 | Q-017 |
| TEST-020 | ten sam config + SHA256 + polityki → identyczny wynik | tests/spec/test_determinism_spec.py::test_identical_outputs_except_timestamp | dwa runy (też z permutacją kluczy configu) → pliki bajtowo identyczne poza timestampem i nazwą katalogu | REPRO-006 | — |
| TEST-021 | walk-forward nie widzi przyszłego okna | tests/spec/test_walk_forward_spec.py::test_no_future_data_in_training | syntetyczne ceny; ceny okna test zastąpione wartościami zatrutymi → wybrane parametry identyczne jak bez zatrucia | WF-004, WF-014 | Q-022 |
| TEST-022 | obcięcie do wspólnego zakresu raportowane | tests/spec/test_calendar_spec.py::test_common_range_truncation_reported | źródło A 2000–2020, B 2005–2018 → zakres 2005–2018; pola w summary i wiersze validation_report | NORM-011 | Q-009 |
| TEST-023 | signal-only od 1920; pełny backtest przed VII 1926 wymaga innego pliku | tests/spec/test_calendar_spec.py::test_signal_only_1920_full_backtest_needs_returns | staged stock signal (1920) + FF (1926-07): komenda `signals` od 1920 działa; `run` od 1920 → czytelny błąd; z syntetycznym stocks-return od 1920 → działa | NORM-012 | Q-011, Q-012, Q-038 |
| TEST-024 | suma tax_events = total tax paid | tests/spec/test_metrics_spec.py::test_tax_events_sum_equals_summary | run individual_pl z dywidendami, RF, CG, daniną i terminalem → Σ(category=tax) = total_tax_paid; sumy per typ = MET-016..019 | MET-015, TAX-004 | Q-035 |
| TEST-025 | FF sobota → poprzedni piątek | tests/spec/test_calendar_spec.py::test_ff_saturday_mapping | 19260710 → 1926-07-09 (nie 1926-07-16); regresja: 1158 wierszy staged różni się od mapowania naiwnego | NORM-013, NORM-018 | — |
| TEST-026 | FF czwartek świąteczny → piątek tego tygodnia | tests/spec/test_calendar_spec.py::test_ff_thursday_mapping | czwartek przed Wielkim Piątkiem → piątek tego tygodnia; także 1929-11-27 (śr) → 1929-11-29, 2001-09-10 (pn) → 2001-09-14 | NORM-013 | — |
| TEST-027 | parser FF: tylko rekordy YYYYMMDD | tests/spec/test_parsers_spec.py::test_ff_parser_nondata | plik staged: 5226 rekordów, pierwszy 19260702, ostatni 20260828, 7 linii nie-danych (linie 1–5, 5232, 5233); wariant syntetyczny z dodatkową stopką | NORM-014, SCHEMA-002 | Q-021 |
| TEST-028 | CPI 2025-10 previous_available | tests/spec/test_parsers_spec.py::test_cpi_missing_policy | staged CPI: 2025-10 = 324.800 (z 2025-09) z flagą imputacji; polityka error → błąd; run nominalny niezablokowany | NORM-015, SEM-010 | Q-045 |
| TEST-029 | wagi dryfują bez rebalancingu | tests/spec/test_rebalancing_spec.py::test_weight_drift | signal-only, 2 aktywa 50/50, zwroty +10%/−10% → 55/45 i dalej dryf; brak transakcji do jawnego eventu | REB-002, PORT-005 | — |
| TEST-030 | band 1 pp = punkt procentowy | tests/spec/test_rebalancing_spec.py::test_band_rebalance_pp | targety 60/20/20; odchylenie 1.00 pp → trigger w T+1; 0.99 pp → brak; 1% względne (0.6 pp) → brak | REB-007 | Q-039 |
| TEST-031 | confirmation stocks=2, gold=4 niezależne od delay | tests/spec/test_signals_spec.py::test_confirmation_defaults | domyślny config; te same warunki przy delay 1 i 3 → te same tygodnie potwierdzenia, wykonania przesunięte o delay | SIG-012, SIG-013, SIG-016 | — |
| TEST-032 | detal: RF +19%, ujemny RF bez ulgi | tests/spec/test_tax_individual_spec.py::test_rf_tax_individual | rf 100 000, R_rf=0.001 → podatek 19; R_rf=−0.0002 → 0; dotyczy rf_base i rezerw | IND-014, IND-015, PORT-013 | — |
| TEST-033 | terminal liquidation realizuje wszystko | tests/spec/test_tax_individual_spec.py::test_terminal_liquidation | otwarte loty: zysk 100 000 i strata 30 000 → realizacja 70 000 → CG 13 300; after-tax wealth niższy; ścieżka tygodniowa bez zmian | IND-016, IND-020 | Q-032 |
| TEST-034 | carry-forward 5 lat i wygasanie | tests/spec/test_tax_individual_spec.py::test_loss_carryforward_5y | strata 100 000 w 2010; zyski 30 000/rok 2011..2016 → offset oldest-first do 2015; bucket wygasa przed 2016; wariant fraction=0.5 | IND-010, IND-013 | Q-030, Q-031 |
| TEST-035 | fundacja: koszt 40 000 wg timingu i proration | tests/spec/test_tax_foundation_spec.py::test_foundation_admin_cost | run 2018-03..2020-06: 2018 proporcjonalnie, płatne w 1. tygodniu 2019; 2019 pełne w 1. tygodniu 2020; 2020 proporcjonalnie w terminalu; brak dla none/individual | FND-010, FND-011, FND-012, FND-014 | Q-034 |
| TEST-036 | Sharpe/Sortino/Calmar wg wzorów | tests/spec/test_metrics_spec.py::test_metric_conventions | zwroty [0.01, −0.02, 0.015, 0.0, −0.005], RF 0.0005/tydz.; wartości oczekiwane policzone ręcznie w fixture | MET-006..MET-010 | Q-040 |
| TEST-037 | WF continuous_state | tests/spec/test_walk_forward_spec.py::test_state_continuity_between_windows | dwa okna OOS; otwarte loty i bucket straty na granicy → przeniesione; NAV ciągły; należność za rok przełomu zapłacona raz | WF-004, WF-013 | Q-022 |
| TEST-038 | plik dywidend: ciągła seria Friday od 1970-01-02 | tests/spec/test_parsers_spec.py::test_dividend_input_file | staged plik od 1970-01-02: piątki, brak luk, wzór ≤1e-12, statusy ∈ {actual, estimate} | DIV-012, SEM-007 | Q-008 |
| TEST-039 | plik BTC: ciągła seria calendar-week z Sunday close | tests/spec/test_parsers_spec.py::test_btc_input_file | staged BTC po normalizacji (Q-005/Q-006): klucz piątek, close niedziela, delta 2, weekly_return = iloraz, 794 wiersze 2011-07-08..2026-09-18; osobno test surowej konwencji poniedziałkowej | SCHEMA-004, SEM-008 | Q-005, Q-006 |
| TEST-040 | domyślne progi | tests/spec/test_signals_spec.py::test_default_thresholds | domyślny config: stocks 0, gold 0, btc 0.03; dodatkowe aktywo XYZ → 0.03 | SIG-004, DEF-003, DEF-030..032 | — |
| TEST-041 | stan początkowy z historii, split RISK_OFF | tests/spec/test_signals_spec.py::test_initial_state_reconstruction | historia kończąca się RISK_OFF → sleeve 50/50 (sell_fraction 0.5) bez transakcji na starcie; brak resetu do RISK_ON | SIG-003, SIG-018 | Q-012, Q-019 |
| TEST-042 | warm-up MA + confirmation + delay | tests/spec/test_signals_spec.py::test_warmup_confirmation_delay | historia = minimum → OK; minimum−1 → WarmupError; pierwsze możliwe potwierdzenie dokładnie w tygodniu indeksu ma+N−2 (0-based) | NORM-010, ERR-003 | Q-012, Q-013 |
| TEST-043 | etykieta real CAGR US CPI | tests/spec/test_metrics_spec.py::test_real_cagr_label | domyślny CPI → summary zawiera 'Real returns deflated by US CPI; not Polish CPI' i etykietę 'US CPI-U / CPIAUCNS' | REAL-004 | Q-045 |
| TEST-044 | FF CSV = ZIP | tests/spec/test_parsers_spec.py::test_ff_csv_zip_equivalence | ZIP budowany w czasie testu ze staged CSV (tmp) → rekordy znormalizowane identyczne | DATA-010, DATA-002 | Q-031 |
| TEST-045 | pierwszy tydzień okresu po Friday key | tests/spec/test_rebalancing_spec.py::test_calendar_rebalance_week_key | tydzień Mon 2020-12-28 / Fri 2021-01-01 → trigger roczny i kwartalny w tym tygodniu; granice miesięcy analogicznie | REB-006 | Q-039 |
| TEST-046 | należności roczne: krok 2 ustala, krok 3 płaci | tests/spec/test_rebalancing_spec.py::test_annual_cashflow_timing | przełom roku z należnością CG i kosztem fundacji: A) bez triggera → sell_to_pay; B) z rebalancingiem rocznym → targety od NAV−należności, zapłata z wpływów sprzedaży | PORT-011, TAX-006, IND-006, FND-011 | Q-029 |
| TEST-047 | koszty także dla sell_to_pay i terminal | tests/spec/test_costs_spec.py::test_all_trade_cost_scope | bps>0; każda transakcja (signal, rebalance, sell_to_pay, terminal) ma koszt >0 | REB-011 | Q-017 |
| TEST-048 | podatki terminalne bez skoku na ścieżce | tests/spec/test_metrics_spec.py::test_terminal_tax_excluded_from_nav_path | individual z dużym niezrealizowanym zyskiem: after_tax_terminal_wealth < pre_terminal_nav; ostatni punkt ścieżki = pre_terminal_nav; maxDD bez zmian | PORT-014, MET-025, MET-026 | Q-032 |
| TEST-049 | niepełne ostatnie okno OOS zachowane | tests/spec/test_walk_forward_spec.py::test_partial_last_window_reported | syntetyczny zakres 23 lata, train 15, test 5 → okna 15–20 i 20–23; ostatnie z faktyczną długością w walk_forward_results.csv | WF-015, WF-016 | Q-020, Q-022 |
| TEST-050 | dywidendy tylko reinvest | tests/spec/test_dividends_spec.py::test_dividend_reinvest_only | config `tax.dividend_reinvest: cash` → ConfigError; CLI nie ma takiej opcji | DIV-006 | — |
| TEST-051 | niepełny bieżący tydzień odrzucony | tests/spec/test_no_lookahead_spec.py::test_incomplete_current_week_drop | kopia pliku akcji + wiersz week_start 2026-09-21; as_of 2026-09-22 → odrzucony, nie w SMA; as_of 2026-09-25 → przyjęty | NORM-019, SEM-011 | Q-010 |
| TEST-052 | fundacja: setup cost 40 000 przed alokacją | tests/spec/test_tax_foundation_spec.py::test_foundation_setup_cost | 4 profile, ten sam config: fundacje startują z initial−40 000, none/individual z initial; setup ≥ initial → błąd; brak kosztów transakcyjnych od setup | FND-015, FND-016 | Q-033 |
| TEST-053 | kolejność sell_to_pay | tests/spec/test_rebalancing_spec.py::test_sell_to_pay_funding_order | należność 50: rf_base 30 → rezerwy 10/30 pro rata (5/15); większa należność → aktywa pro rata do wartości; permutacje kolejności aktywów → identyczny wynik | TAX-006 | Q-046 |
| TEST-054 | rebalancing netto po amounts_due | tests/spec/test_rebalancing_spec.py::test_rebalance_net_of_amounts_due | tydzień rebalancingu z należnością 10 000 i 10 bps → wagi końcowe = targety (≤1e-12) na NAV netto po należnościach i kosztach | TAX-007 | Q-018 |

## 3. Testy jednostkowe (tests/unit)

| Plik | Zakres |
|---|---|
| test_config.py | defaulty DEF-001..033 (parametryzowane po ID), precedencja CLI > plik > defaulty, YAML = JSON, jednostki CLI (Q-026), defaulty per komenda (Q-027), waluta PLN, brak FX |
| test_calendar.py | `friday_key`, `week_start+4`, agregacja daily→weekly, granice okresów, mapowanie start/end (Q-014), kompletność NORM-019 + availability BTC |
| test_availability.py | `available_at` per źródło, DataView rzuca przy dostępie do przyszłości, przypisanie tygodnia BTC |
| test_data_loader.py | każdy parser (poprawne i uszkodzone pliki), aliasy plików (Q-001), ERR-001, extra assets, distribution file, cache (jeśli nie odroczony) |
| test_validation.py | duplikaty, brakujące tygodnie, polityki `missing.*` (Q-012), numeryka, ERR-002/003, wymagalność plików BTC/dywidend |
| test_signals.py | pasma, progi asymetryczne, próg 0 |
| test_confirmation.py | liczniki i reset, osobne confirm on/off, niezależność aktywów, rekonstrukcja na segmencie ciągłym |
| test_scheduling.py | dokładne przesunięcie delay, kolejka FIFO, no-op, stan efektywny |
| test_allocation.py | targety, single-asset, split początkowy, target_fraction_of_sleeve |
| test_rf.py | rf_base vs rezerwy, net RF wg profilu |
| test_ledger.py | sumy sleeve'ów, dziennik transakcji |
| test_cost_basis.py | FIFO, average_cost, częściowa sprzedaż, lot z dywidendy |
| test_costs.py | koszt, slippage, gross-up |
| test_tax.py | profile, netting, loss buckets, polityki estimate, brak inferencji FF−SPX, tryby zdarzeń fundacji |
| test_settlement.py | kolejność terminalna, bazy dystrybucji, warstwy fundacji |
| test_rebalancing.py | tryby, zachowanie splitu, targety na sumach sleeve'ów |
| test_engine.py | kolejność kroków (trace), brak transakcji przed startem, re-entry tylko z rezerwy, brak powtórnej sprzedaży, podatek zmniejsza NAV w tygodniu płatności |
| test_metrics.py | każdy wzór MET-* na ręcznych danych, turnover, lata kalendarzowe, rolling |
| test_reporting.py | kolumny i formatowanie, tabela konsolowa, etykieta CPI |
| test_optimizer.py | parser zakresów, gridy, cele, limit DD, tie-break, progres |
| test_walk_forward.py | konstrukcja okien rolling/anchored, krok, ostatnie okno, parametry optymalizowane vs stałe |
| test_architecture.py | granice modułów (tax nie importuje engine, signals nie importuje CPI), brak `work/src` w sys.path |

## 4. Testy integracyjne (tests/integration)

* `test_cli.py`: CLI-001..011 (na fixture'ach lub danych staged z markerem `realdata`), w tym
  oczekiwane błędy: CLI-007 bez wag (Q-024), CLI-011 na danych staged (Q-020), S06 bez wag (Q-023; sesja 9: S06 używa zamrożonego configu V2, `tax-compare` bez wag nadal daje ALLOC-001).
* `test_outputs.py`: schematy wszystkich plików wyjściowych, roundtrip `config_resolved.yaml`,
  pola `data_manifest.json`, `validation_report.csv`, typy zdarzeń podatkowych, `weekly_normalized.csv`.
* `test_data_resolution.py`: rozwiązywanie domyślnych plików przez aliasy z raportem (Q-001).
* `test_real_data.py`: ponowne potwierdzenie faktów audytu (korelacja FF vs indeks akcji 0.99 na
  poprawnym indeksie, segmenty złota i akcji w manifeście, pierwszy/ostatni rekord FF).

## 5. Testy właściwości i niezmienników (tests/property)

| Plik | Właściwość |
|---|---|
| test_accounting_identity.py | po każdym kroku: suma aktywów + rf_base + rezerwy = NAV (względnie 1e-10); brak wartości ujemnych |
| test_no_lookahead.py | zaburzenie wszystkich danych po tygodniu T nie zmienia żadnego zdarzenia ani NAV do T; sygnał w T nie zmienia ekspozycji T |
| test_zero_cost_conservation.py | przy 0 bps i profilu none realokacje (signal, rebalance) zachowują NAV dokładnie |
| test_asset_order_invariance.py | permutacja kolejności aktywów w configu i słownikach → identyczne wyniki |
| test_parallel_grid.py | `--jobs 1` i `--jobs N` → identyczne, identycznie uporządkowane `grid_results.csv` |
| test_reproducibility.py | identyczny config + SHA256 → identyczne pliki (poza timestampem) |
| test_terminal_separation.py | settlement nie zmienia żadnego punktu ścieżki tygodniowej |
| test_pre_after_tax.py | dla tax.profile=none ścieżki pre-tax i after-tax są identyczne (Q-015) |
| test_engine_properties.py | silnik: tożsamość NAV po każdym kroku, transakcja 0 bps nie zmienia NAV, sygnał jednego aktywa nie zmienia innych sleeve'ów, przyszłość nie zmienia przeszłości, kolejność kluczy aktywów bez wpływu, zakup z rezerwy nigdy nie tworzy ujemnej gotówki, konserwacja asset↔reserve przy 0 bps |
| test_gap_history_convergence.py | dla startów ≥1936 stan sygnału akcji z całej historii jest identyczny jak z historii po luce 1933 (luka nie zniekształca późniejszych sygnałów; Q-012) |

Generatory danych losowych mają stałe ziarno (`random.Random(seed)`), bez zewnętrznych bibliotek.

## 6. Testy no-look-ahead (zbiorczo)

TEST-002, TEST-018, TEST-021, TEST-051, `test_no_lookahead.py`, `test_availability.py`
(DataView), asercja `execution_week > confirm_week` w scheduling, rekonstrukcja stanu przed OOS
wyłącznie na danych sprzed okna (WF-014), reguła kompletności BTC z `close_date` (Q-007).

## 7. Testy deterministyczności

TEST-020, `test_reproducibility.py`, `test_parallel_grid.py`, `test_asset_order_invariance.py`;
stabilne formatowanie liczb; brak zależności od zegara (as_of jawny w testach).

## 8. Testy walk-forward

TEST-021, TEST-037, TEST-049, `test_walk_forward.py`, CLI-011 (oczekiwany czytelny błąd na danych
staged, pełny przebieg na syntetycznym zakresie 25 lat), `walk_forward_rebalance` przy zmianie wag
i stanu sygnału (Q-022).

Zrealizowane w sesji 12: `tests/spec/test_walk_forward_spec.py` (TEST-021, TEST-037, TEST-049),
`tests/unit/test_walk_forward.py` (grid CLI-011 = 32 448, tylko listowane parametry, gridy i
jednostki, okna rolling/anchored, krok całkowity/ułamkowy, ostatnie okno, luki kalendarza,
niemutowalność snapshotów i memo, sąsiedzi gridu) i `tests/integration/test_walk_forward_e2e.py`
(stała selekcja = jeden ciągły run dla none/individual_pl/fundacji z przeniesionym triggerem
band i granicami noworocznymi, brak terminal settlement na granicach, rebalance przy zmianie wag
i wyjściu aktywów z rezerwą, anulowanie starych oczekujących wykonań przy zmianie parametrów,
kontynuacja żywego trackera, wyjścia i manifest, serial vs parallel, CLI-011 staged = błąd Q-020,
CLI-011 syntetyczne 26.5 roku = 2 pełne + 1 częściowe okno OOS).

## 9. Testy podatkowe

TEST-010..017, TEST-024, TEST-032..035, TEST-046..048, TEST-050, TEST-052..054 oraz
`test_tax.py`, `test_settlement.py`, `test_cost_basis.py`. Kombinacje profili: none,
individual_pl, family_foundation_15, family_foundation_19; każdy test podatkowy sprawdza też
zapis zdarzeń w `tax_events.csv`.

## 10. Testy parserów i calendar alignment

TEST-007, TEST-008, TEST-025..028, TEST-038, TEST-039, TEST-044, TEST-051 oraz
`test_data_loader.py`, `test_calendar.py`, `test_real_data.py`: FF regex i preambuła, mapowanie
sobota/czwartek/środa/poniedziałek, week_start+4 dla akcji i złota, konwencja BTC, zgodność kluczy
między źródłami, luka 1933-03-10 i 2011-06-24, CPI 2025-10.

## 11. Kryterium ukończenia

Wiersz MUST w compliance matrix może dostać `PASS` dopiero, gdy jego test(y) przechodzą, a
powiązane pytania BLOCKER są rozstrzygnięte. Po każdej iteracji:
`python work/tools/check_audit_consistency.py --allow-pass` i pełny `pytest`.
