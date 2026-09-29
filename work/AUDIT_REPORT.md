# AUDIT_REPORT — audyt specyfikacji 3.1 i danych wejściowych (Backtest V2, clean-room)

Data audytu / `as_of` użyte przez narzędzia: **2026-09-29**. Źródła dowodów: wyłącznie pakiet
clean-room (`input/`, dokumenty w katalogu głównym). Nie korzystano z V1 w żadnej formie.
Core engine nie został napisany; powstały wyłącznie narzędzia audytowe, raporty i plany.

## 1. Zakres i metoda

* Przeczytane w całości: `CLEANROOM_POLICY.md`, `AGENTS.md`, `HANDOFF_PROMPT.md`,
  `ARCHITECTURE_GUIDE.md`, `DATA_MAP.csv`, `README.md`, `PACKAGE_METADATA.json`,
  `AB_SCENARIOS.csv` oraz wszystkie 379 wierszy `input/python_backtest_specification_v3_1.csv`.
* `python tools/verify_inputs.py` → PASS (8 plików, SHA256 zgodne);
  `python tools/init_compliance.py` → 379 wymagań (352 MUST, 27 SHOULD). Oba bez błędów.
* Nowe narzędzia (tylko stdlib, deterministyczne wyjście, `work/tools/`):
  * `audit_inputs.py` → `work/audit/input_profile.json`, `input_checks.csv` (58 kontroli),
    `scenario_feasibility.csv` (9 scenariuszy AB + 11 przykładów CLI),
  * `audit_spec.py` → `work/audit/spec_inventory.json`, `spec_config_keys.csv`,
    `spec_cross_references.csv`,
  * `check_audit_consistency.py` — krzyżowa weryfikacja macierzy, pytań, planów i liczb w tym raporcie.
* Wyniki: `work/compliance_matrix.csv` (mapa wszystkich 379 wymagań), `work/implementation_questions.csv`
  (49 pytań), `work/IMPLEMENTATION_PLAN.md`, `work/TEST_PLAN.md`.

## 2. Liczby

| Miara | Wartość |
|---|---|
| Wszystkie wymagania | 379 |
| MUST | 352 |
| SHOULD | 27 |
| Pytania/konflikty ogółem | 49 w audycie, 50 po dodaniu Q-050 (sesja 2) |
| **BLOCKER** (severity z audytu) | **6** (Q-002, Q-004, Q-005, Q-008, Q-012, Q-020) |
| — po adjudykacji: DATA BLOCKER | 3 (Q-002, Q-004, Q-008) |
| — po adjudykacji: RESOLVED | 3 blokery (Q-005, Q-012, Q-020) + Q-006 |
| **MAJOR** | **20** |
| MINOR | 23 (+ Q-050) |
| MUST zmapowane bez blokera (`MAPPED`) | 340 w audycie → 347 po adjudykacji |
| MUST zablokowane decyzją (`BLOCKED`) | 12 w audycie → 5 po adjudykacji (tylko DATA BLOCKER) |
| MUST `PASS` | 0 w audycie; 139 po sesji 2; 165 po sesji 3 (sekcja 13) |
| SHOULD proponowane do odroczenia | 1 (ERR-007 cache) |
| Kontrole danych | 58: 27 PASS, 12 FAIL, 14 WARN, 5 INFO |
| Scenariusze AB + przykłady CLI | 20: 10 wykonalnych, 10 wymaga decyzji |

Definicje: **BLOCKER** — wymaganie MUST (lub jego test) nie może przejść na dostarczonych
danych/specyfikacji bez decyzji użytkownika lub poprawki; **MAJOR** — istnieje rozsądna
interpretacja, ale istotnie wpływa na wyniki lub dowód zgodności; **MINOR** — doprecyzowanie
o małym wpływie, propozycja zostanie zastosowana i udokumentowana.

## 3. Audyt danych — podsumowanie per źródło

| Rola | Plik staged | Wiersze / zakres (Friday key) | Konwencja daty | Najważniejsze ustalenia |
|---|---|---|---|---|
| stocks signal | `US_STOCK_PRICE_WEEKLY_REAL_1919_2026.csv` | 5568; 1920-01-02..2026-09-18 | `week_start` (pn) → +4 | Identyczny z plikiem Schwert do 1962-06-25, splice od 1962-07-02 (spec: 1928); brak kolumny segmentu; luka 1933-03-10; brak częściowego tygodnia 2026-09-21; indeks nominalny mimo „REAL” w nazwie |
| stocks return (FF) | `F-F_Research_Data_Factors_weekly.csv` | 5226; 1926-07-02..2026-08-28 | YYYYMMDD: 3912 pt, 1158 sob, 152 czw, 3 śr, 1 pn | 5 linii przed danymi (spec: 4), pusta nazwa kolumny daty; mapowanie same-week daje unikalne klucze; luka 1933-03-10; 8 tygodni z ujemnym RF; korelacja z indeksem akcji 0.990 (naiwne mapowanie 0.591; 1926–1952: 0.992 vs 0.100) |
| gold | `GOLD_REAL_weekly_1970_2026.csv` | 2959; 1970-01-09..2026-09-18 | `week_start` (pn), spec: `week_end` | Źródła TVC/OANDA/FOREXCOM_FILL — **nie LBMA PM**; start 1970, nie 1968; 26 wierszy FILL 1997–98; zwroty zgodne z cenami; klucze = klucze FF |
| btc | `BTC_REAL_weekly_2010_2026.csv` | 845; 2010-07-16..2026-09-25 | `date` = poniedziałek = `source_week_start`; `close_date` = date+6 (niedziela) | Spec: date piątek, close = date+2; luka 2011-06-24 i zwrot 2-tygodniowy w 2011-06-27; podzbiór 2011-07-08..2026-09-18 = dokładnie 794 wiersze ze spec |
| dividend | `SPX_dividend_return_weekly_shiller.csv` | 5218; 1926-07-02..2026-06-26 | piątek | Brak 4 z 9 kolumn SCHEMA-005; **100% status=estimate**; wartości stałe w miesiącu (Shiller); wzór dokładny (2e-16); `spx_close_prev` = indeks akcji z poprzedniego piątku 5217/5217; kończy się 9 tygodni przed FF |
| cpi | `CPIAUCNS.csv` | 1364; 1913-01..2026-08 | 1. dzień miesiąca | Zgodny ze spec; jedyny brak 2025-10 (zgodnie z SEM-010) |
| supplemental | `US_STOCK_PRICE_WEEKLY_schwert_1919_1962.csv` | 2218; 1919-12-29..1962-07-02 | `week_start` | Bez klucza configu; posłużył jako dowód punktu splice |

Pozytywne potwierdzenia (PASS): zgodność SHA256; brak duplikatów we wszystkich plikach; zwroty
złota i BTC równe ilorazom cen; `dividend_return = dividend_points/spx_close_prev`; klucze
tygodni FF = akcje = złoto w części wspólnej; mapowanie FF wg NORM-013 potwierdzone empirycznie
(dokładnie 1158 przesuniętych wierszy przy mapowaniu naiwnym, zgodnie z notatką NORM-018);
CPI ciągły. Relacje dat: stocks/gold `week_end = week_start + 4`; BTC `week_key = date + 4`,
`close_date = week_key + 2`; dywidendy datowane piątkiem tego samego tygodnia co FF.

No-look-ahead na tych danych jest spełnialne: każda obserwacja ma jednoznaczny tydzień
kalendarzowy i znany moment dostępności (piątek; BTC niedziela). Jedyna luka w regule
specyfikacji dotyczy kompletności BTC przy `as_of` w piątek/sobotę (Q-007).

## 4. Blokery (6) — stan po adjudykacji w sekcji 12

1. **Q-002 — złoto nie jest LBMA PM i zaczyna się w 1970** (SEM-003, DATA-003, SIG-009).
   Mechanika (ta sama seria dla sygnału i zwrotu) jest implementowalna, ale SEM-003 nie przejdzie.
   Decyzja: dostarczyć plik LBMA PM od 1968 albo formalnie przyjąć staged serię jako proxy.
2. **Q-004 — splice sygnału akcji w 1962, nie w 1928; brak metadanych segmentów** (SEM-001).
   Propozycja: jawnie zadeklarowane segmenty w konfiguracji danych V2 (Schwert zweryfikowany
   bajt-w-bajt do 1962-06-25) + ostrzeżenie. Decyzja: poprawka spec albo nowy plik.
3. **Q-005 — konwencja daty BTC (poniedziałek, close +6) vs spec (piątek, close +2)**
   (SCHEMA-004, SEM-008, TEST-039). Propozycja: jawny parametr `calendar.btc_date_convention`,
   `week_key = date + 4` w obrębie tego samego tygodnia (bez przesunięcia między tygodniami).
   Wymaga akceptacji, bo TEST-039 dosłownie dotyczy „załączonego pliku”.
4. **Q-008 — plik dywidend: 5 zamiast 9 kolumn, wszystkie statusy `estimate`, miesięczne
   wygładzenie** (SCHEMA-005, SEM-007, DIV-011, DIV-009). Propozycja: 5 kolumn wymaganych,
   4 opcjonalne, status przenoszony dosłownie; `spread_annual_dps` odroczone.
5. **Q-012 — luki historyczne (1933-03-10 w akcjach i FF, 2011-06-24 w BTC) vs „cała historia”
   i domyślne polityki `error`** (SIG-003, NORM-010, NORM-004/005). Dosłownie każdy run
   z sygnałem akcji lub BTC kończy się błędem. Propozycja: polityki egzekwowane na zakresie
   konsumowanym; rekonstrukcja stanu na najdłuższym ciągłym segmencie przed startem (raportowane);
   `carry` jako jawna alternatywa.
6. **Q-020 — CLI-011/S09 niewykonalne na danych**: z BTC w gridzie wag globalny zakres po
   warm-upie to 2012-09-28..2026-08-28 = 13.93 roku < 15 lat treningu → 0 okien OOS.
   Decyzja: zmienić parametry S09 (np. `--btc-weight 0`, krótszy trening) albo zaakceptować
   czytelny błąd jako poprawną obsługę.

## 5. Major issues (20)

| ID | Temat | Propozycja (skrót) |
|---|---|---|
| Q-001 | Domyślne nazwy plików nie istnieją | jawna tabela aliasów rola→plik staged, raportowana w manifeście |
| Q-003 | Złoto `week_start` zamiast `week_end` | `week_end = week_start + 4`, konwencja w manifeście |
| Q-006 | BTC: zakres, luka 2011, 845 vs 794 wiersze | domyślnie historia od 2011-07-08 (odtwarza SEM-008) |
| Q-007 | NORM-019 vs dostępność BTC w niedzielę | kompletność wymaga też `available_at <= as_of` |
| Q-009 | Dywidendy kończą się 2026-06-26; kiedy plik jest wymagany | wymagany gdy podatek od dywidend > 0; wtedy obcina wspólny zakres z raportem |
| Q-013 | Warm-up złota dla startu 1971 (51 < 55) | ERR-003 domyślnie; jawny opt-in fallback RISK_ON z ostrzeżeniem |
| Q-014 | Mapowanie `--start/--end`, NAV_start, elapsed_days | pierwszy Friday key ≥ start; inception = piątek wcześniej; elapsed = 7·N dni |
| Q-015 | Definicja ścieżki pre-tax | cienista symulacja z podatkami = 0 i tymi samymi kosztami |
| Q-016 | Cost basis reinwestowanych dywidend | nowy lot o koszcie = dywidenda netto |
| Q-017 | Koszty na nogach RF, turnover, alokacja początkowa | RF = gotówka bez kosztów; koszty tylko na aktywach ryzykownych |
| Q-019 | Stan początkowy vs oczekujące wykonania przy delay>1 | split wg stanu efektywnego; wykonania ≥ start w kolejce |
| Q-022 | Semantyka walk-forward (gridy per aktywo, granice okien, ostatnie okno) | wspólny grid; rekonstrukcja + `walk_forward_rebalance`; reszta = osobne okno |
| Q-023 | tax-compare bez wag; `configs/portfolio.yaml` poza clean-room | błąd ALLOC-001; wagi od użytkownika w V2-owym configu |
| Q-026 | Jednostki CLI (procenty) vs config (ułamki) | CLI procenty dla progów/gridów wag; config zawsze ułamki |
| Q-032 | Terminal settlement per profil, baza `gain_only` | likwidacja terminalna dla wszystkich profili; gain vs initial_capital |
| Q-033 | NAV_start fundacji (setup cost) w CAGR | CAGR od initial_capital (koszt widoczny) |
| Q-034 | Proration kosztu admin fundacji | dni kalendarzowe w (inception, last_key] |
| Q-035 | Koszty w `tax_events.csv` vs TEST-024 | kolumna `category`; TEST-024 sumuje tylko `tax` |
| Q-036 | Schemat pliku dla trybu dividend `exact` | `pay_date,dividend_return` pod osobnym kluczem |
| Q-037 | Tryb fundacji `distribution_schedule` bez semantyki | czytelny błąd do czasu decyzji; propozycja semantyki w Q-037 |

## 6. Najważniejsze niejednoznaczności

1. Gdzie specyfikacja deklaruje fakty o danych, których dane nie potwierdzają (LBMA, splice 1928,
   konwencja BTC, statusy dywidend, zakresy dat) — Q-002, Q-004, Q-005, Q-006, Q-008, Q-009.
2. „Cała historia” (SIG-003) vs domyślne `error` dla braków (NORM-004/005) — Q-012.
3. ERR-003 (błąd warm-up) vs SIG-003 (fallback RISK_ON z ostrzeżeniem) — Q-013.
4. Mapowanie dat startu/końca na tygodnie i `elapsed_days` (wpływa na każdy CAGR) — Q-014.
5. Definicja „pre-tax” dla runów z podatkami — Q-015.
6. Traktowanie RF (gotówka czy instrument) w kosztach i turnover — Q-017.
7. Reinwestycja dywidend w cost basis — Q-016.
8. Terminal settlement dla profilu `none` i fundacji (`gain_only`) — Q-032, Q-033.
9. Semantyka walk-forward przy zmianie parametrów sygnału między oknami — Q-022.
10. Jednostki w CLI i kolidujące domyślne wartości kluczy `signal.delay_weeks`,
    `signal.threshold_grid`, `data.cpi_file` — Q-026, Q-027.
11. Wymagania SHOULD, bez których nie przejdą MUST (DATA-010, IND-010, SIG-017, RISK-008,
    FND-009, META-005, REAL-005, DIV-009, OPT-009) — Q-031; plan: implementować jak MUST.

## 7. Co można implementować bez dodatkowych decyzji

* Szkielet CLI/importu, config z defaultami DEF-*, precedencja, `config_resolved.yaml`.
* Kalendarz (Friday key, mapowanie FF, `week_start+4`, granice okresów), parser FF (CSV/ZIP),
  CPI z `previous_available`, walidacje duplikatów/luk/numeryki, manifest SHA256.
* Sygnały: SMA, progi, histereza, confirmation z resetem, delay, kolejka wykonań.
* Księgowość: sleeve'y, rezerwy RF, ledger, FIFO/average cost, koszty, tożsamość NAV.
* Rebalancing (kalendarzowy, band, od NAV netto), sell_to_pay, pipeline PORT-011.
* Podatki detalu i fundacji w trybie `terminal`, loss buckets, danina, metryki, reporting,
  optymalizator gridów i scany, walk-forward (na danych syntetycznych).
* Wszystkie MAJOR/MINOR mają propozycje, które można zaimplementować za przełącznikami configu;
  ich potwierdzenie nie zmienia architektury.

## 8. Co jest obecnie zablokowane konfliktem specyfikacji z danymi

* Audyt: 12 wierszy MUST `BLOCKED`. Po adjudykacji (sekcja 12) zostaje 5 wierszy MUST `BLOCKED`
  przez DATA BLOCKER: SEM-003 (Q-002); SEM-001 (Q-004); SCHEMA-005, SEM-007, TEST-038 (Q-008).
  Pozostałe (Q-005, Q-012, Q-020) są rozstrzygnięte i wróciły do `MAPPED`.
* Scenariusze wymagające decyzji (z `scenario_feasibility.csv`):

| Scenariusz | Problem | Pytania |
|---|---|---|
| S01 / CLI-001 | warm-up złota 51 < 55 tygodni przed 1971-01-01 | Q-013, Q-002 |
| S05 / CLI-005 | dywidendy kończą się 2026-06-26 < `--end` 2026-07-31 (5 tygodni); nazwa pliku z CLI nie istnieje | Q-009, Q-001, Q-008 |
| S06 / CLI-006 | brak wag (ALLOC-001), `configs/portfolio.yaml` poza clean-room; bez `--start` zakres obejmuje lukę 1933 | Q-023, Q-025, Q-012 |
| S09 / CLI-011 | 0 okien OOS (13.93 roku < 15 lat treningu) | Q-020, Q-022 |
| CLI-007 | brak wag → błąd wg ALLOC-001 | Q-024 |
| CLI-008 | bez `--start` zakres zawiera lukę 1933-03-10 → błąd przy domyślnej polityce | Q-025, Q-012 |

Wykonalne bez decyzji (poza ogólnymi Q-001/Q-012 dla historii sygnałów): S02, S03, S04, S07, S08
oraz CLI-002, CLI-003, CLI-004, CLI-009, CLI-010.

## 9. Proponowana kolejność implementacji

1. Decyzje użytkownika w sprawie 6 blokerów (równolegle z krokami 2–5, które od nich nie zależą).
2. Szkielet: `backtest.py`, pakiet `src`, `models`, `config`, `cli`, pytest.
3. Dane: `calendar`, `data_loader`, `validation`, `availability`, `weekly_normalized.csv`
   + testy parserów i kalendarza.
4. Sygnały: `signals`, `confirmation`, `scheduling`, komenda `signals`.
5. Księgowość bez podatków: `ledger`, `rf`, `cost_basis`, `costs`, `allocation`, `engine`
   + testy właściwości (tożsamość NAV, no-look-ahead, niezmienność kolejności aktywów).
6. `rebalancing`, `sell_to_pay`.
7. `tax`, `settlement` (profile none/individual/fundacje).
8. `metrics`, `reporting`, `manifest`; determinizm.
9. `optimizer` (gridy, scany, tax-compare, `--jobs`).
10. `walk_forward`.
11. Pełne testy, statusy PASS w macierzy, `IMPLEMENTATION_NOTES.md`, `python tools/freeze_v2.py`.

## 10. Odtworzenie audytu

```bash
python tools/verify_inputs.py
python work/tools/audit_inputs.py            # --as-of 2026-09-29 domyślnie
python work/tools/audit_spec.py
python work/tools/check_audit_consistency.py
```

## 11. Gotowość do implementacji

Repozytorium jest gotowe do rozpoczęcia implementacji warstw niezależnych od blokerów
(kroki 2–6 z sekcji 9). Pełna zgodność (wszystkie MUST = PASS i freeze) wymaga rozstrzygnięcia
6 blokerów; do tego czasu odpowiadające im wiersze pozostają `BLOCKED`, a V2 będzie emitować
jawne ostrzeżenia lub czytelne błędy zamiast cichych założeń.

## 12. Adjudykacje użytkownika (sesja 2)

| Pytanie | Status | Skutek |
|---|---|---|
| Q-002 złoto | DATA_BLOCKER | Loader/sygnał/zwrot wg LBMA PM (DATA-003, SIG-009, SEM-003) testowane syntetycznym LBMA; staged TVC/OANDA tylko jako jawne proxy (`canonical=false`). SEM-003 pozostaje BLOCKED. |
| Q-004 splice akcji | DATA_BLOCKER | Wsparcie segmentów źródłowych i kanonicznej kompozycji Schwert < 1928 + SPX ≥ 1928 z rebasingiem; staged plik z ostrzeżeniem o provenance. SEM-001 pozostaje BLOCKED. |
| Q-005 BTC | RESOLVED | Normalizacja raw Monday→Friday (`week_key = source_week_start + 4`, `available_at = close_date = week_key + 2`), zakres kanoniczny 2011-07-08..2026-09-18 (794 wiersze). |
| Q-006 zakres BTC | RESOLVED | Wynika z Q-005. |
| Q-008 dywidendy | DATA_BLOCKER | Loader kanoniczny wymaga pełnego SCHEMA-005; staged plik tylko przez adapter proxy. SCHEMA-005, SEM-007 i TEST-038 pozostają BLOCKED. |
| Q-012 luki kalendarza | RESOLVED | Cała historia do rekonstrukcji; luka tygodnia przerywa liczniki confirmation; brak auto forward-fill; polityki `error/drop/carry` tylko dla brakującego źródła w tygodniu kalendarza runu; wspólne luki raportowane. |
| Q-020 walk-forward | RESOLVED | `train_years=15` bez skracania; brak pełnego okna → „insufficient history for requested walk-forward training window”. |
| Q-017 koszty i RF (sesja 3) | RESOLVED | RF = ledger gotówkowy; koszty/slippage tylko na kupnie/sprzedaży stocks/gold/btc; sprzedaż `net = traded*(1-tc-slip)`, zakup z gotówki `traded = C/(1+tc+slip)`; początkowa alokacja nie jest transakcją. |
| Q-019 stan początkowy (sesja 3) | RESOLVED | Stan potwierdzony ≠ efektywny; split z efektywnego; wykonania ≥ start pozostają pending i są transakcjami w backteście. |
| Q-049 tożsamość NAV (sesja 3) | RESOLVED | `abs(NAV - sum(components)) / max(1, abs(NAV)) <= 1e-10`; NAV z komponentów przez `math.fsum`. |
| Q-050 SMA przez lukę (sesja 3) | RESOLVED | SMA z ostatnich `ma` dostępnych obserwacji; luka flagowana, przerywa liczniki, nie zeruje historii SMA; warm-up liczy obserwacje. |

Wiersze MUST nadal `BLOCKED` (dane niekanoniczne): SEM-001, SEM-003, SCHEMA-005, SEM-007, TEST-038.

## 13. Stan implementacji

* Sesja 2: warstwa foundation/core (config, CLI, modele, kalendarz, dostępność, loadery,
  walidacja, sygnały, alokacja, komenda `signals`).
* Sesja 3: centralny silnik portfela bez podatków i bez rebalancingu strategicznego
  (`ledger`, `costs`, `rf`, `cost_basis`, `engine`), pipeline PORT-011 z rzeczywistymi punktami
  rozszerzeń dla kroków 0/2/3/5/6, komenda `run` dla `tax.profile=none` i `signal-only`.

Stan macierzy: MUST — 165 `PASS`, 29 `IN_PROGRESS`, 153 `MAPPED`, 5 `BLOCKED` (DATA BLOCKER),
0 `FAIL`; SHOULD — 10 `PASS`. Testy: `python -m pytest work` (wszystkie przechodzą). Żaden wiersz
zablokowany przez Q-002/Q-004/Q-008 nie został oznaczony `PASS` na podstawie staged plików;
dowody dla LBMA, segmentów SEM-001 i kanonicznego SCHEMA-005 pochodzą z syntetycznych fixture'ów.

<!-- AUDIT_COUNTS
requirements_total=379
must=352
should=27
questions_total=50
blockers=6
major=20
minor=24
must_mapped=153
must_blocked=5
must_pass=165
should_proposed_deferral=1
input_checks=58
checks_fail=12
checks_warn=14
checks_pass=27
checks_info=5
scenarios_total=20
scenarios_feasible=10
scenarios_needs_decision=10
blocker_ids=Q-002,Q-004,Q-005,Q-008,Q-012,Q-020
data_blocker_ids=Q-002,Q-004,Q-008
resolved_ids=Q-005,Q-006,Q-012,Q-017,Q-019,Q-020,Q-049,Q-050
open_questions=39
must_fail=0
must_in_progress=29
should_pass=10
-->
