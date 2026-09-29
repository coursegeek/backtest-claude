# IMPLEMENTATION_NOTES — Backtest V2 (clean-room)

Stan: **foundation/core** (sesja 2). Zaimplementowane: CLI i API importu, konfiguracja z
precedencją i jednostkami, modele, kalendarz, dostępność informacji, loadery z normalizacją
kanoniczną, walidacja (w tym semantyka luk Q-012), pipeline sygnałów z rekonstrukcją stanu
i opóźnieniem, podstawowe modele alokacji/sleeve'ów, komenda `signals`.
Nie ma jeszcze: silnika portfela (PORT-011), ledgera/kosztów/podatków, rebalancingu,
metryk, summary/trades/tax_events, optymalizatora, walk-forward. Komendy portfelowe
rozwiązują i walidują config, po czym kończą się kodem 3 z jawnym komunikatem.

Źródło prawdy dla statusów: `compliance_matrix.csv`; pytania: `implementation_questions.csv`.

## Uruchamianie

```bash
python -m pip install pytest pyyaml          # zależności: stdlib + PyYAML; pytest do testów
python -m pytest work                        # (pytest.ini: testpaths = tests)
python work/backtest.py signals --asset btc --as-of-date 2026-09-29
python work/backtest.py run --weights stocks=0.4,gold=0.4,rf=0.2 --print-config
python work/tools/check_audit_consistency.py --allow-pass
```

## Układ kodu

`work/backtest.py` (CLI + fasada importu) → `src/cli.py` → `src/app.py` (komendy).
Warstwy: `models`, `errors`, `config`, `calendar`, `availability`, `data_loader`, `validation`,
`signals`, `confirmation`, `scheduling`, `signal_analysis`, `allocation`, `manifest`,
`reporting`. `src` jest pakietem importowanym jako `src.*` (Q-044).

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
7. **SMA przez lukę (Q-050, OPEN MINOR)**: SMA to średnia ostatnich `ma` obserwacji; warm-up
   liczy obserwacje przed startem.
8. **Warm-up (Q-013 propozycja)**: za mało historii → `WarmupError` (ERR-003); jawne
   `signal.initial_state=RISK_ON` zamienia błąd w ostrzeżenie.
9. **Stan początkowy (Q-019)**: stan efektywny = wykonania zaplanowane przed pierwszym tygodniem;
   późniejsze pozostają w kolejce (`PreStartState.pending`). Brak potwierdzonej zmiany w historii
   → ostrzeżenie `initial_state_fallback` (SIG-003).
10. **Harmonogram (Q-043)**: FIFO, wykonanie przypadające na tydzień luki odbywa się w pierwszym
    obserwowanym tygodniu ≥ execution_week; wykonanie do bieżącego stanu to no-op (flaga).
11. **Tożsamość NAV (Q-049)**: tolerancja względna 1e-10.
12. **Determinizm**: kanoniczna kolejność aktywów, `math.fsum` w SMA, `repr` dla floatów,
    YAML/JSON z sortowanymi kluczami; jedyne różnice między identycznymi runami to znacznik
    czasu w manifeście i nazwa katalogu wyników.

## Poprawki względem audytu

* Plik FF ma 7 linii nie-danych (audyt w TEST_PLAN/Q-021 liczył 8 przez pusty element po
  końcowym CRLF); poprawione.
