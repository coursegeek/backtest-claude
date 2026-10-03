# IMPLEMENTATION_PLAN — Backtest V2 (clean-room)

Status: **plan architektury**. W tej sesji nie powstaje core engine. Dokument jest normatywnie
podrzędny wobec `input/python_backtest_specification_v3_1.csv`; tam gdzie plan przyjmuje
interpretację, wskazuje pytanie z `work/implementation_questions.csv` (Q-xxx).
Mapa każdego wymagania na moduł/test jest w `work/compliance_matrix.csv`.

## 1. Zasady projektowe

1. **Jedna deterministyczna funkcja przejścia tygodnia** (`engine.step`) używana przez `run`,
   `signals`, `delay-scan`, `threshold-scan`, `optimize`, `rebalance-scan`, `tax-compare`
   i walk-forward. Optymalizator nigdy nie duplikuje logiki portfela.
2. **Event/state-machine**: stan portfela i stan sygnałów są jawnymi, niemutowalnymi
   migawkami (`dataclass(frozen=True)`); każdy krok zwraca nowy stan + listę zdarzeń
   (transakcje, podatki, koszty, sygnały) z typem i okresem.
3. **Jawny model czasu**: `week_key` (piątek tygodnia Monday–Sunday), `available_at`
   (kiedy informacja jest znana), `execution_week` (kiedy zaplanowana akcja może się wykonać).
4. **Determinizm**: brak zegara ściennego w obliczeniach (as_of i timestamp tylko w manifeście),
   kanoniczna kolejność aktywów (`stocks, gold, btc`, dalej dodatkowe alfabetycznie) we wszystkich
   pętlach i sumach, float64 z ustaloną kolejnością operacji, formatowanie liczb `repr`
   (najkrótszy round-trip). Kolejność kluczy w configu/słownikach nie wpływa na wynik.
5. **Rozdzielność**: sygnały, księgowość, podatki, metryki, raportowanie, walidacja
   i optymalizacja to osobne moduły z jednokierunkowymi zależnościami (sekcja 3).
6. **Zależności**: biblioteka standardowa + PyYAML (dostępny w środowisku). `pytest` jako
   zależność deweloperska (do zainstalowania). Brak pandas w rdzeniu. numpy tylko jeśli
   profilowanie gridów walk-forward tego wymaga i tylko przy identycznych wynikach.
7. **Pakiet `src`**: `work/src/__init__.py`, import wyłącznie jako `src.<moduł>`;
   `work/src` nigdy nie trafia do `sys.path`, więc `src/calendar.py` nie przesłania
   modułu standardowego `calendar` (Q-044). `work/backtest.py` = CLI + fasada importu.
8. **Brak cichych założeń**: każda interpretacja danych (konwencja daty BTC, mapowanie
   `week_start` złota, obcięcie historii przed luką, aliasy plików) jest parametrem configu,
   trafia do `config_resolved.yaml`, `data_manifest.json` i `validation_report.csv`.

## 2. Moduły i odpowiedzialności

| Concern (zakres) | Moduł | Odpowiedzialność | Kluczowe wymagania | Czego NIE robi |
|---|---|---|---|---|
| config / CLI | `backtest.py`, `src/cli.py`, `src/config.py` | komendy `run`, `signals`, `delay-scan`, `threshold-scan`, `optimize`, `rebalance-scan`, `tax-compare`; defaulty DEF-*, precedencja CLI > plik > defaulty, konwersja jednostek, `config_resolved.yaml` | META-001/004/005, DEF-*, ALLOC-001..003, RUN-*, CLI-* | obliczeń |
| błędy | `src/errors.py` | hierarchia wyjątków z kodami wyjścia CLI (ERR-001..004, ConfigError, LookAheadError, NotImplementedCommand) | ERR-* | logiki |
| komendy | `src/app.py` | implementacje komend za CLI i API importu: `signals`, `run` (przygotowanie danych `prepare_run` → `PreparedRun`; wykonanie `run_prepared`: engine + hooki + settlement + shadow pre-tax + metryki + wyjścia); pozostałe komendy walidują config i kończą się NotImplementedCommand | META-001, NORM-012 | obliczeń |
| tax-compare (sesja 9) | `src/tax_compare.py` | orkiestrator: walidacja listy profili i kolejność kanoniczna, configi profili różniące się wyłącznie `tax.profile`, superset wymagań danych, jedno `prepare_run` i ten sam `PreparedRun` dla każdego `run_prepared`, top-level `summary.csv` + `profiles/<p>/` + `tax_compare_manifest.json`, atomowość błędów (Q-023) | TAX-003, FND-008, CLI-006, REP-002/012, REPRO-006 | własnego silnika, ładowania danych, rankingu |
| signal pipeline | `src/signal_analysis.py` | złożenie signals → confirmation → scheduling, rekonstrukcja stanu przed startem, analiza signal-only | SIG-003/016/018/019, NORM-012 | księgowości |
| modele | `src/models.py` | typy: `Observation`, `SourceSeries`, `SignalParams`, `SignalState`, `ScheduledExecution`, `Lot`, `Trade`, `TaxEvent`, `PortfolioState`, `WeekRecord`, `RunResult` | ARCH-* | logiki |
| data loading / normalization | `src/data_loader.py` | parsery ról: stocks signal, FF CSV/ZIP, gold, BTC, dywidendy smoothed/exact, CPI, extra assets, distribution file; rozwiązywanie ścieżek i aliasów (Q-001); proweniencja (SHA256, zakres, liczba wierszy, segmenty źródeł) | DATA-*, SCHEMA-*, NORM-001/006/008/009/014/015, PORT-001..004, SEM-* | decyzji o polityce braków |
| calendar | `src/calendar.py` | `friday_key`, mapowanie FF same-calendar-week, `week_start+4`, agregacja daily→weekly, granice okresów po Friday key, wspólny zakres (NORM-011), mapowanie `--start/--end` (Q-014), kompletność tygodni (NORM-019, Q-007) | NORM-007/008/009/011/013/019/020, REB-006, RUN-001/002 | dostępności informacji |
| information availability | `src/availability.py` | `available_at` każdej obserwacji (FF/akcje/złoto: piątek; BTC: `close_date`), `DataView(as_of)` blokujący dostęp do przyszłości, `execution_week = confirm_week + delay` i asercja `execution_week > confirm_week` | META-003, NORM-016/017, SEM-009, PORT-012, WF-014 | sygnałów |
| validation | `src/validation.py` | schematy, duplikaty, brakujące tygodnie, polityki `missing.*` (Q-012), numeryka, walidacja plików BTC/dywidend, FF alignment regression, warm-up (ERR-003, Q-013), wymagalność plików (DATA-005/007, Q-009), wiersze `validation_report.csv` | NORM-001..006/010/018, DIV-010/012, ERR-001..004 | naprawiania danych |
| signals | `src/signals.py` | SMA (okno włączne, Q-028), pasma progów per aktywo (sym./asym.), `raw_condition` | SIG-001/004..007, THR-004 | stanu i opóźnień |
| confirmation | `src/confirmation.py` | maszyna stanów RISK_ON/RISK_OFF, liczniki `exit/entry` z resetem, potwierdzenie N tygodni, rekonstrukcja przed startem (SIG-003, SIG-019) | SIG-003/006/008/011..017/019, NORM-010 | wykonania transakcji |
| delay | `src/scheduling.py` | kolejka FIFO wykonań `T+delay` (Q-043), stan efektywny vs potwierdzony (Q-019), zmiana parametrów na granicy okna WF (Q-022) | SIG-016, DELAY-002/004 | kwot transakcji |
| allocation | `src/allocation.py` | wagi strategiczne sleeve'ów, single-asset, split początkowy wg stanu efektywnego (SIG-018), `target_fraction_of_sleeve` (Q-042) | ALLOC-*, PORT-009/010, SIG-018, RISK-008 | handlu |
| RF reserves | `src/rf.py` | `rf_base` i `rf_reserve_<asset>` jako ekwiwalent gotówki (Q-017); zwrot RF i net RF wg profilu | ALLOC-004, RISK-002/006, PORT-002/013 | podatków rocznych |
| ledger | `src/ledger.py` | pozycje per sleeve, dziennik transakcji z `reason`, przepływy gotówki, tożsamość NAV (RISK-004, tolerancja względna Q-049) | PORT-005..007/009, TAX-005 | wyboru lotów |
| FIFO cost basis | `src/cost_basis.py` | loty FIFO i średni koszt, realizacja zysków/strat, loty z reinwestycji dywidend (Q-016) | PORT-008, IND-008/011 | stawek podatku |
| transaction costs | `src/costs.py` | `cost = traded_value*bps/10000`, slippage, zakres kosztów REB-011 | REB-003/004/011 | decyzji o handlu |
| tax (taxation) | `src/tax.py` | interfejs `TaxPolicy`; profile `none`, `individual_pl`, `family_foundation_15/19`; podatki tygodniowe (dywidendy, RF), roczny netting, loss buckets 5 lat, danina, koszty fundacji (setup/admin + proration), zdarzenia `TaxEvent` z `category` (Q-035) | TAX-*, DIV-*, IND-*, FND-* | wykonywania sprzedaży |
| foundation (sesja 8) | `src/foundation.py` | profile `family_foundation_15/19` (tax_event=terminal): `FoundationParams`, `FoundationState`, `FoundationHooks` - setup cost w kroku 0, roczny koszt admin (Q-034) jako AmountsDue w kroku 2, podatek od dywidend/RF przez wspólny prymityw kroku 5; bez loss buckets i CG | FND-001..016, DIV-003 | finansowania należności (krok 3) |
| terminal settlement | `src/settlement.py` | likwidacja terminalna z kosztami, rozliczenie ostatniego roku, podatek od dystrybucji fundacji; osobno od ścieżki tygodniowej (Q-032) | PORT-014, IND-016/017/020, FND-003..007 | ścieżki NAV |
| rebalancing | `src/rebalancing.py` | triggery weekly/monthly/quarterly/annually/band/signal-only, rebalancing od NAV netto po `amounts_due` z punktem stałym kosztów (TAX-007, Q-018), zachowanie splitu RISK_OFF (REB-009) | REB-*, TAX-007, PORT-010 | finansowania bez triggera |
| sell_to_pay | `src/sell_to_pay.py` | finansowanie należności bez triggera: A `rf_base` → B rezerwy pro rata → C aktywa ryzykowne pro rata (gross-up kosztów, Q-046) → błąd insolvency | TAX-006 | zmiany stanu sygnału |
| weekly engine | `src/engine.py` | inicjalizacja (krok 0), `step()` wg PORT-011, pętla tygodni, cienista symulacja pre-tax (Q-015), wywołanie settlement | PORT-011/012, SIG-019, RISK-001/003/005/007 | parsowania i raportów |
| metrics | `src/metrics.py` | CAGR, after-tax CAGR, real, vol, Sharpe, Sortino, maxDD, Calmar, lata kalendarzowe, turnover, czas w stanie, rolling | MET-*, REAL-001..003, REB-005 | I/O |
| reporting | `src/reporting.py`, `src/manifest.py` | `summary.csv`, `weekly_portfolio.csv`, `trades.csv`, `tax_events.csv`, `signals.csv`, `config_resolved.yaml`, `data_manifest.json`, `validation_report.csv`, `grid_results.csv`, `walk_forward_results.csv`, `weekly_normalized.csv`, `rolling_metrics.csv`, tabela w konsoli | REP-*, REPRO-*, NORM-007 | obliczeń metryk |
| scany (sesja 10) | `src/scans.py` | `delay-scan`, `threshold-scan`, `rebalance-scan`: rozwiązanie i walidacja całego gridu przed danymi, configi wariantów różniące się tylko skanowanym parametrem, jedno `prepare_run` z największym warm-upem gridu, `run_prepared` na punkt, `grid_results.csv` (kolumny scanu + `SUMMARY_FIELDS`), `scan_manifest.json`, atomowość, postęp na stderr | DELAY-001..005, THR-001..005, REB-010, REP-010, ALLOC-002, CLI-002/003/010 | własnego silnika, ładowania danych per punkt, celu/rankingu |
| optimization (sesja 11) | `src/optimizer.py` | in-sample: grid wag (procenty, Q-026) i walidacja sum (Q-041) przed danymi, unia aktywów przygotowana raz, `run_prepared` na kandydata (pula procesów `--jobs`), statusy, mapa celów, limit DD, tie-break, `grid_results.csv` + `summary.csv` + `selected/` + `optimizer_manifest.json`; parser zakresów `a:b:step` jest w `src/config.py` | OPT-001..010, TEST-009, ERR-005/006, REPRO-006 | własnego silnika, ładowania danych per kandydat |
| walk-forward | `src/walk_forward.py` | okna rolling/anchored, krok, ostatnie okno częściowe, wybór parametrów na train, sklejanie OOS z ciągłym stanem, `walk_forward_rebalance` | WF-*, REP-016 | danych z okna test w treningu |

### Graf zależności (bez cykli)

```
models
  └─ calendar ─ availability
       └─ data_loader ─ validation
signals ─ confirmation ─ scheduling
allocation, rf, costs, cost_basis ─ ledger
tax (czyta ledger/cost_basis przez interfejsy) ─ settlement
rebalancing, sell_to_pay (ledger + costs + cost_basis)
engine (orkiestracja wszystkich powyższych)
metrics (czyste funkcje na RunResult)   reporting/manifest (I/O)
optimizer ─ walk_forward (wywołują wyłącznie engine)
app (prepare_run / run_prepared) ─ tax_compare, scans, optimizer (orkiestracja; bez engine/tax/settlement)
config/cli/backtest.py (wejście)
```

## 3. Model czasu i information availability

* `week_key` = piątek tygodnia Monday–Sunday zawierającego datę źródłową (NORM-007).
  FF: `d - weekday(d) + 4` (NORM-013); pliki `week_start`: `+4` (NORM-008; dla złota Q-003);
  BTC: `date` (poniedziałek w staged pliku) `+4` przy konwencji `week_start_monday` (Q-005).
* `available_at`: akcje/FF/złoto = `week_key` (zamknięcie piątkowe); BTC = `close_date`
  (niedziela, NORM-016); dywidendy smoothed = `week_key` (używane tylko do podatku kroku 5,
  nie do decyzji); CPI nie jest wejściem decyzji (SEM-004).
* Decyzja dla tygodnia T (kroki 1–3) używa wyłącznie informacji z `available_at <=` niedziela
  tygodnia T-1. Ocena sygnału na końcu T (krok 6) używa informacji z `available_at <=` niedziela T.
* Zwrot tygodnia T jest stosowany do wartości po transakcjach kroku 1–3 (PORT-012), więc
  żaden sygnał z T nie zmienia ekspozycji tygodnia T (TEST-002).
* Kompletność: rekord istnieje w obliczeniach, gdy `week_key <= min(end, as_of)`
  i `available_at <= as_of` (NORM-019 + Q-007); odrzucone wiersze → `validation_report.csv`
  i `dropped_incomplete_weeks` w summary.
* Egzekwowanie: engine czyta dane przez `availability.DataView(t)`, który rzuca wyjątek przy
  próbie dostępu do obserwacji z `available_at` > t; test właściwości zaburza przyszłe ceny
  i sprawdza niezmienność przeszłych decyzji.

## 4. Centralny tygodniowy pipeline (PORT-011)

```
init(resolved_config, normalized_data) -> PortfolioState S0
  0a. common range + warm-up check (NORM-011, NORM-010/ERR-003, Q-012/Q-013/Q-014)
  0b. rekonstrukcja sygnałów na historii przed startem: SMA, liczniki, stan potwierdzony,
      stan efektywny, kolejka wykonań >= start (SIG-003, SIG-019, Q-019) — bez ledgera
  0c. fundacja: investable = initial_capital - setup_cost (FND-015/016), zdarzenie kosztowe
  0d. alokacja początkowa wg allocation.targets i splitu stanu efektywnego (SIG-018);
      pierwsze loty; nie jest transakcją kosztową (Q-017)

for each week T (Friday key) in [first_week, last_week]:
  1. execute_scheduled_signal_trades(T)                     # scheduling + engine + costs + cost_basis
       RISK_ON->RISK_OFF: sprzedaj sell_fraction bieżącej wartości aktywa → rf_reserve_<asset>
       RISK_OFF->RISK_ON: kup aktywo za całą rf_reserve_<asset> (po kosztach) → rezerwa = 0
       tylko aktywo sygnałowe i jego rezerwa (RISK-003); realizacja FIFO w roku week_key (Q-029)
  2. amounts_due(T)                                          # tax
       jeśli T to pierwszy tydzień z Friday key w roku Y+1:
         individual_pl: CG + danina za Y (netting, loss buckets) ; fundacja: admin cost za Y
  3. if rebalance_trigger(T):                                # rebalancing
         NAV_net = NAV_after_signal - amounts_due; targety od NAV_net po kosztach (punkt stały);
         sprzedaże → zapłata amounts_due → zakupy; split RISK_OFF zachowany (TAX-007, REB-009)
     elif amounts_due > 0:
         sell_to_pay: rf_base → rezerwy pro rata → ryzykowne pro rata (TAX-006)
     zapłata zmniejsza NAV w T (TAX-005); zdarzenia tax/cost z datą płatności
  4. apply_returns(T)                                        # engine + rf
       wartość_i *= (1 + R_i); akcje: składnik cenowy (R_total - d) + dywidenda brutto d
  5. immediate_taxes(T)                                      # tax
       dividend_tax = stock_value_start * d * rate; netto reinwestowane (nowy lot, Q-016)
       rf_tax = max(0, rf_value_start * R_rf) * rate dla rf_base i każdej rezerwy
  6. end_of_week(T)                                          # signals + confirmation + scheduling
       SMA/pasma/raw_condition z danych dostępnych do końca T; liczniki; potwierdzenia;
       wykonanie na T+delay; ocena odchyleń band dla triggera w T+1 (REB-007);
       WeekRecord (wartości sleeve'ów start/end, wagi actual/target, zwroty, podatki, stany)
     invariant: NAV == sum(ryzykowne) + rf_base + sum(rezerwy)  (RISK-004, tolerancja względna)

after last week:
  terminal_settlement(S_last)  # settlement: osobno od ścieżki (PORT-014, IND-020, Q-032)
     likwidacja z kosztami → rozliczenie roku końcowego / koszt admin pro rata →
     podatek od dystrybucji fundacji → after_tax_terminal_wealth
```

Każdy krok emituje zdarzenia z polem `pipeline_step`, co pozwala testowi
`test_pipeline_order_trace` sprawdzić dokładną kolejność, a komparatorowi A/B znaleźć pierwszą
rozbieżność na warstwach: normalized data → signals → scheduled executions → trades → taxes →
holdings → weekly NAV → metrics.

Symulacja pre-tax (Q-015) to drugi przebieg tej samej funkcji `step` z podatkami = 0.

## 5. Podsystem sygnałów (signals, confirmation, delay)

* Parametry per aktywo (SIG-008) rozstrzygane: CLI > `signals.<asset>.*` > `signal.*` >
  `signals.default.*` > DEF (Q-027). Progi w configu dziesiętnie, w CLI w procentach (Q-026).
* `raw_condition_t ∈ {BELOW_LOWER, INSIDE, ABOVE_UPPER}` względem `SMA_t*(1∓p)`; dla p=0
  nierówności ostre (THR-004), równość = INSIDE (histereza SIG-006).
* Stan RISK_ON: `exit_counter` rośnie tylko przy BELOW_LOWER, inaczej 0; po `confirm_off`
  → potwierdzenie RISK_OFF w T. Symetrycznie RISK_OFF (SIG-015, SIG-017).
* Potwierdzenie planuje wykonanie w `T+delay` (DELAY-002/004); kolejka FIFO, wykonanie do stanu
  równego bieżącemu efektywnemu jest no-op (Q-043).
* Rekonstrukcja przed startem działa na najdłuższym ciągłym segmencie historii przy polityce
  `error` lub na całej historii przy `carry` (Q-012). Warm-up = `ma + max(confirm) + max(delay)`.

## 6. Księgowość: ledger, RF reserves, FIFO cost basis, koszty

* Sleeve aktywa ryzykownego = aktywo + `rf_reserve_<asset>` (PORT-009); wagi strategiczne
  dotyczą sum sleeve'ów (PORT-010); `rf_base` to osobny sleeve (ALLOC-004).
* Pozycja aktywa jest prowadzona jako loty (jednostki × indeks ceny aktywa); wartość = suma
  lotów; dla akcji indeks cenowy tygodnia = `1 + R_total - d`, a dywidenda netto kupuje
  nowe jednostki (Q-016). FIFO/average_cost (IND-008) realizuje zysk przy każdej sprzedaży
  (IND-011), także sell_to_pay i terminalnej.
* RF sleeves są ekwiwalentem gotówki; koszty/slippage dotyczą każdej nogi stocks/gold/btc
  (Q-017, REB-011); `traded_value` = wartość brutto aktywa.
* Tożsamość NAV sprawdzana po każdym kroku; ujemna gotówka/sleeve = błąd.

## 7. Podatki (tax) i koszty fundacji

* `TaxPolicy` (TAX-001) z metodami: `setup_cost`, `weekly_dividend_tax`, `weekly_rf_tax`,
  `annual_amounts_due(year)`, `terminal(state)`. Stawki wyłącznie z configu (META-006).
* `individual_pl`: dywidendy 19% tygodniowo (DIV-002/005), RF 19% od dodatniego dochodu
  (IND-014/015), CG 19% od rocznej dodatniej podstawy po nettingu stocks+gold+btc
  (IND-001/019) i loss buckets 5 lat oldest-first z `loss_offset_fraction` (IND-010/013, Q-030),
  danina 4% ponad 1 mln PLN od CG netto + external base (IND-002..005/018),
  należność za Y w kroku 2 pierwszego tygodnia Y+1 (IND-006, Q-029).
* `family_foundation_15/19`: dywidendy 15% (FND-001), internal trading tax (default 0; > 0 rocznie
  bez carry-forward, Q-047), RF rate (default 0, FND-013), setup cost przed alokacją
  (FND-015/016), admin cost 40 000 PLN za rok z proration (FND-010..012, Q-034), podatek od
  dystrybucji terminalnie (FND-003..007, Q-032) albo przy każdej wypłacie z harmonogramu
  `distribution_schedule` (FND-005/009, Q-037; zrealizowane w sesji 14).
* `none`: brak podatków, brak kosztów fundacji.
* Każde zdarzenie: `date, event_type, asset, tax_base, rate, tax_due, category, settlement,
  period, dividend_status` (TAX-004, REP-015, DIV-011, Q-035).

## 8. Rebalancing i sell_to_pay

* Trigger kalendarzowy: pierwszy tydzień, którego Friday key wpada w nowy miesiąc/kwartał/rok
  (REB-006); band: na końcu T `|w - w*| >= band_pp/100` dla któregokolwiek sleeve'a
  (REB-007, Q-039); `signal-only`: brak rebalancingu strategicznego (REB-002).
* Rebalancing: wszystkie sleeve'y do targetów (REB-008) od NAV netto po `amounts_due`
  i kosztach (punkt stały, Q-018), split RISK_OFF skalowany proporcjonalnie (REB-009).
* sell_to_pay: deterministyczny, niezależny od kolejności kluczy (sortowanie kanoniczne),
  gross-up kosztów, bez zmiany stanu sygnału, insolvency error (TAX-006, Q-046).

## 9. Metryki

Czyste funkcje na ścieżkach NAV (MET-*): CAGR z `365.2425/elapsed_days` i `elapsed_days`
wg Q-014; vol/Sharpe/Sortino tygodniowe annualizowane `sqrt(52)`, `ddof=1`; maxDD na running max
z NAV_start (Q-040); Calmar pre-tax i after-tax (ścieżka bez skoku terminalnego, MET-025/026);
real CAGR z CPI (REAL-001, Q-045); turnover, liczba transakcji, czas RISK_ON/OFF, lata
kalendarzowe, rolling (MET-022/023).

## 10. Reporting i manifest

* Katalog `results/<timestamp>_<run_name>/` (REP-001); wszystkie pliki poza timestampem
  identyczne dla identycznego configu i SHA256 wejść (REPRO-006).
* Pliki: `summary.csv`, `weekly_portfolio.csv`, `trades.csv`, `tax_events.csv`, `signals.csv`,
  `config_resolved.yaml`, `data_manifest.json`, `validation_report.csv`, `grid_results.csv`,
  `walk_forward_results.csv`, `weekly_normalized.csv` (NORM-007), `rolling_metrics.csv`.
* Manifest: SHA256, zakres dat, liczba rekordów, konwencje dat, segmenty źródeł (SEM-001/003),
  aliasy plików, git commit, timestamp Europe/Warsaw, as_of (REPRO-001..007).

## 11. Optimization (optymalizacja, scany, tax-compare)

* Gridy: parser `a:b:step` włącznie oraz list (DELAY-003, THR-005); wagi w procentach;
  `stocks=remainder`; odrzucanie sum > 100% (OPT-006, TEST-009) ze statusem w `grid_results.csv`.
* Cele OPT-007, limit maxDD OPT-008, tie-break OPT-009 (Q-041); cały grid zapisany (OPT-010).
* Równoległość `--jobs` (ERR-006): zadania indeksowane, wyniki scalane po indeksie → wynik
  identyczny z serialnym; postęp tylko na stderr (ERR-005).
* `delay-scan`, `threshold-scan`, `rebalance-scan` (sesja 10, `src/scans.py`, Q-026/Q-027
  RESOLVED): orkiestratory produkcyjnej ścieżki `run`, nie część optimizera. Cały grid
  rozwiązany i zwalidowany przed danymi, dane przygotowane raz (warm-up = największe wymaganie
  gridu), jeden pełny run na punkt (`run_prepared`), `grid_results.csv` = kolumny scanu +
  `SUMMARY_FIELDS` (pre-tax i after-tax, DELAY-005), bez celu i rankingu.
* `tax-compare` (sesja 9, `src/tax_compare.py`, Q-023 RESOLVED): nie jest częścią optimizera ani drugim backtesterem - to orkiestrator produkcyjnej ścieżki `run`. Wejście przygotowane raz dla supersetu wymagań danych wybranych profili (plik dywidend w kalendarzu, gdy wymaga go choć jeden profil), ten sam `PreparedRun`/`EngineInputs` dla każdego profilu, jeden `summary.csv` (wiersz na profil, schema `SUMMARY_FIELDS`), bez rankingu.

## 12. Walk-forward

Zrealizowane w sesji 12 (`src/walk_forward.py`, Q-022 RESOLVED):

* Grid tylko z `--optimize-params` (wagi Q-041 x ma x threshold x delay x confirmation x
  sell_fraction x rebalance_band), jedna wartość dla wszystkich aktywnych aktywów, walidacja
  przed danymi; dane przygotowane raz (unia aktywów, maksymalny warm-up gridu).
* Okna: `rolling|anchored`, `train_years` (pełne, Q-020), `test_years`, `step_years`
  (kalendarzowy lub dzienny), ostatni segment do końca danych zawsze zachowany, faktyczna
  długość (WF-011/012/015/016).
* Trening wyłącznie na `PreparedRun.training_view` (tygodnie, rynek i obserwacje sygnału z
  `week_key`/`available_at < test_start`, bez CPI; WF-004, TEST-021) tą samą logiką selekcji co
  in-sample optimizer.
* OOS: jedna ciągła ścieżka (`engine.EngineStart`; portfel, loty, loss buckets, stan
  podatkowy/fundacji, trackery sygnału, trigger band - WF-013, TEST-037); stan sygnału dla
  nowych parametrów ze snapshotu treningu, dla niezmienionych kontynuacja żywego trackera
  (WF-014); `walk_forward_rebalance` w kroku 3 pierwszego tygodnia okna przy zmianie wag,
  aktywów, rebalancingu lub stanu (TAX-007 z należnościami tygodnia); jeden terminal settlement
  po ostatnim tygodniu OOS; ciągły pre-tax shadow.
* Brak wystarczającej historii → czytelny błąd (CLI-011 na danych staged, Q-020).

## 13. Obsługa błędów

`DataFileNotFound(path, config_key)` (ERR-001), `MissingColumns(file, missing)` (ERR-002),
`WarmupError(asset, available, required)` (ERR-003), `DividendModeError` (ERR-004),
`ConfigError` (waluta, wagi, jednostki), `InsolvencyError` (TAX-006), `LookAheadError`
(asercja availability). Kod wyjścia CLI ≠ 0 i komunikat z nazwą klucza configu.

## 14. Decyzje (stan po adjudykacji)

RESOLVED: Q-005 (normalizacja BTC Monday→Friday, zakres SEM-008), Q-006, Q-012 (luki kalendarza:
cała historia, luka przerywa liczniki, polityki tylko dla brakującego źródła w tygodniu kalendarza
runu), Q-020 (brak skracania okna treningowego), Q-022 (walk-forward: niezależny TRAIN, ciągły
OOS, sekcja 12), Q-011 (historia akcji od 1920-01-02), Q-013 (ścisły warm-up, jedyny fallback
RISK_ON), Q-024 (CLI-007 bez wag = ALLOC-001), Q-025 (start bez --start po warm-upie, klucze
--data-file), Q-038 (komenda signals), Q-047 (roczny internal trading tax fundacji bez
carry-forward), Q-037 (distribution_schedule: wypłaty brutto, podatek potrącany z wypłaty,
kumulatywna baza gain_only, koniec bez likwidacji), Q-004 i Q-008 (sesja 15: rozwiązane nowymi
danymi kanonicznymi akcji i dywidend). DATA BLOCKER (bez wpływu na implementację, tylko na PASS
wymagań o danych kanonicznych): Q-002 złoto LBMA (SEM-003). Pozostałe pytania MAJOR/MINOR są implementowane wg `proposed_interpretation`
za przełącznikami configu i opisane w `IMPLEMENTATION_NOTES.md`.

## 15. Kolejność implementacji (postęp: kroki 1–4 zrobione w sesjach 2–3, bez podatków w kroku 4)

1. Szkielet: `backtest.py`, `src/__init__.py`, `models`, `config` (DEF-*, precedencja, jednostki), `cli` (parsowanie), pytest.
2. `calendar`, `data_loader`, `validation`, `availability` + testy parserów/kalendarza (TEST-007/008/025..028/038/039/044/051) i `weekly_normalized.csv`.
3. `signals`, `confirmation`, `scheduling` + komenda `signals` (TEST-001/003/004/018/023/031/040..042).
4. `ledger`, `rf`, `cost_basis`, `costs`, `allocation`, `engine` bez podatków (TEST-002/005/006/019/029) + testy właściwości.
5. `rebalancing`, `sell_to_pay` (TEST-030/045/053/054).
6. `tax`, `settlement` (TEST-010..017/032..035/046..048/050/052).
7. `metrics`, `reporting`, `manifest` (TEST-020/022/024/036/043).
8. `tax-compare` (sesja 9, CLI-006); scany (sesja 10, CLI-002/003/010); `optimizer` in-sample (sesja 11, TEST-009, CLI-004/005).
9. `walk_forward` (sesja 12, TEST-021/037/049, CLI-011).
10. Pełny przebieg testów, aktualizacja compliance matrix, `IMPLEMENTATION_NOTES.md`, `python tools/freeze_v2.py`.

## 16. Ryzyka

* **Wydajność**: CLI-011 z domyślnymi gridami to ~32 tys. kombinacji na okno; wymagane
  efektywne `step()` (pre-alokowane tablice, brak kopiowania historii) i `--jobs`.
* **Tolerancje float**: RISK-004 1e-10 interpretowane względnie (Q-049).
* **Dane**: 6 blokerów danych/spec; wyniki scenariuszy S01, S05, S06, S09 zależą od decyzji.
