---
category: Process
description: 'Manual für Projektplanung und Ticket-Qualität: Epics, Stories, vertikaler Schnitt (INVEST/SPIDR), Definition of Ready/Done, Board-Workflow (Kanban, WIP-Limits), Entscheidungs-Management, Regeln für Agent-getriebene Abarbeitung (Claude Code + Codex-Review)'
file_patterns:
    - '**/docs/lessons.md'
    - '**/CLAUDE.md'
last_updated: "2026-09-06"
subcategory: Project Planning
technologies:
    - Nextcloud Deck
    - Claude Code
    - Kanban
topic: projektplanung-tickets
---
# Projektplanung & Tickets — Manual

## 1 · Hierarchie der Arbeit

**Epic → Story/Ticket → (implizite) Tasks.**

- **Epic** = ein Ergebnis, das mehrere Tickets braucht. Pflichtfelder: Ziel als *Zustand* („Fertig ist es, wenn …"), geordnete Ticket-Liste, Verweis auf Basis-/Messkarten. Ein Epic ohne erreichbaren Fertig-Zustand ist keins — auflösen und Karten verteilen (Präzedenz: E3, E6 auf Board 65).
- **Story/Ticket** = ein vertikaler Schnitt, in einem Loop-Durchgang abschließbar, mit eigenem Beleg.
- **Tasks** (Teilschritte innerhalb eines Tickets) bekommen *keine* eigenen Karten — sie leben als Checkliste im Ticket oder in TodoWrite. Karten, die in Wahrheit Tasks sind („Backend-Teil", „Model-Layer"), sind das häufigste Symptom horizontalen Schnitts.
- **Ein Epic zur Zeit.** Neue Wünsche während eines laufenden Epics werden neue Karten; das laufende Epic wird nie verlängert.

## 2 · Vertikaler Schnitt

**Regel: Nach Abschluss eines Tickets kann jemand etwas Neues sehen oder tun.** Horizontal geschnittene „Stories" (Frontend-Story, Backend-Story, DB-Story) sind Tasks, keine Stories — sie liefern einzeln keinen prüfbaren Wert und verstecken fehlenden Fortschritt.

**INVEST als Prüfstein** vor dem Einplanen:
- **I**ndependent — ohne andere offene Karten ziehbar (harte Abhängigkeiten explizit benennen, nicht implizit lassen)
- **N**egotiable — das *Wie* ist verhandelbar, das *Was* fest
- **V**aluable — für sich genommen wertvoll (sichtbar, messbar oder als Risikoabbau begründet)
- **E**stimable — abschätzbar; wenn nicht: erst Spike
- **S**mall — ein Loop-Durchgang (bauen + prüfen + max. 2 Codex-Runden)
- **T**estable — es gibt eine Prüfkante, an der Fertig belegt wird

**SPIDR zum Splitten**, wenn ein Ticket zu groß ist:
- **S**pike — offene Forschungsfrage als eigene, zeitgeboxte Karte; Ergebnis ist Wissen, nicht Code (Präzedenz: Slice 0 Socket-Spike)
- **P**aths — ein Bedienpfad zuerst (erst Happy Path, dann Fehlerpfade)
- **I**nterfaces — eine Plattform/ein Kanal zuerst (erst Mac, dann iOS; erst CloudKit, dann Relay)
- **D**ata — ein Datenausschnitt zuerst (erst Tail, dann Historie)
- **R**ules — Regeln erst vereinfacht, dann verschärft (erst höchstens-einmal, dann Idempotenz)

**Zulässige Ausnahme:** reine Umbauten ohne neue Funktion (Refactoring-Schritt wie E2·3), wenn sie genau *eine* prüfbare Frage beantworten („läuft der bestehende Weg unverändert?"). Auch sie brauchen Akzeptanzkriterien und Prüfkante.

## 3 · Ticket-Format (Template)

```
Titel: beobachtbares Ergebnis, nicht Tätigkeit
       ✅ „Chat öffnet an der neuesten Nachricht"
       ❌ „Scrolling verbessern"

Ziel (1–3 Sätze): was ist nachher anders, für wen.

Kontext / Einstieg:
- betroffene Module/Dateien
- relevante Karten (nur Nummern-Verweise, keine Kopien)
- geltende Vorentscheidungen (Verweis auf Entscheidungskarte)

Akzeptanzkriterien („Fertig ist es, wenn …"):
- [ ] abhakbar, konkret, falsifizierbar
- [ ] inkl. Prüfkante: Fenster-Screenshot / Gerät / zweiter Mac / Test

Scope-Grenze: was ausdrücklich NICHT dazugehört.

Offene Entscheidungen: KEINE (sonst gehört die Karte nicht in Ready).
```

**Definition of Ready (Einzugskriterium für die Ready-Spalte):** alle Template-Felder gefüllt, INVEST bestanden, keine offene Frage an den Menschen, Abhängigkeiten erledigt oder explizit benannt.

**Definition of Done:**
1. Akzeptanzkriterien erfüllt und an der *niedrigsten echten Kante* belegt (echtes Fenster, echtes Gerät, echter Container — nicht nur Unit-Test, wenn eine echtere Kante existiert)
2. Tests grün; keine geratenen Versionen/APIs
3. Codex-Review bestanden (max. 2 Runden; Runde-2-Funde außerhalb des Tickets → neue Karten, nie mitfixen)
4. Erkenntnisse → `docs/lessons.md`; Karte bekommt **kurzen** Abschlussvermerk (3–5 Zeilen: was, Beleg, Commit)
5. Label „Abgeschlossen", Karte → Archiv-Spalte

## 4 · Karte ≠ Journal (wichtigstes Anti-Pattern)

**Eine Karte ist eine Spezifikation mit Abschlussvermerk, kein Arbeitsprotokoll.** Auf Board 65 wuchsen Karten (623, 737, 739) auf 8–12 KB: Bugreport + Messreihen + Codex-Runden + beantwortete und offene Fragen vermischt. Folgen: Agent verbrennt Kontext beim Board-Lesen, „was ist zu tun" ist nicht mehr von „was war" trennbar, Entscheidungsstand driftet.

Regeln:
- Lange Analysen, Messprotokolle, Verlaufsdoku → `docs/` (lessons.md, protokoll-spezifische Dateien); die Karte *verweist*.
- Neue Befunde während der Arbeit, die nicht das Ticket betreffen → **neue Karte**, nicht ans laufende Ticket anhängen.
- Wächst eine Karte über ~1 Bildschirmseite Spezifikation hinaus, ist das ein Split-Signal.

## 5 · Board = Workflow, Labels = Themen

- **Spalten sind Workflow-Zustände**, nicht Themen und nicht Historie: Kontext/Regeln → Entscheidungen → Epics → Ready (priorisiert) → In Arbeit → Review → Blockiert → Später → Archiv.
- **Epics/Themen ausschließlich über Labels** (E1, E2, …). Nie beide Dimensionen in Spalten mischen — das erzeugt zwei konkurrierende Wahrheiten (Board 65: Slice-Spalten vs. Epic-Labels).
- **WIP-Limit 1** in „In Arbeit" (Solo-Agent-Betrieb). Erst fertig machen, dann Neues ziehen. Ready ist priorisiert; immer die oberste Karte ziehen.
- **Blockiert-Karten nennen in der ersten Zeile, worauf sie warten** (Gerät / Entscheidung X / Karte Y). Eine Blockade ohne benannten Blocker ist keine.
- **Erledigtes wandert sofort nach rechts ins Archiv.** Erledigte Karten in Arbeits-Spalten sind Rauschen; auf Board 65 verdeckten ~80 erledigte Karten die ~45 offenen.
- **Physische Reihenfolge pflegen:** `order` bewusst setzen (10er-Schritte). Ein Board voller order=999 hat keine Reihenfolge.
- Board vor jedem Durchgang neu lesen — der Mensch legt laufend Karten an; das Board ist die Wahrheit, nicht das Gedächtnis.

## 6 · Entscheidungen: genau ein Ort

Das teuerste Fehlermuster auf Board 65: dieselbe Frage auf 2–3 Karten (602/685/764), Antwort landet auf einer davon, die anderen bleiben „offen" → Widerspruch, Nachfragen, Schleifen.

Regeln:
- **Eine Entscheidung = eine Karte** in der Entscheidungen-Spalte. Andere Karten verweisen per Nummer, kopieren nie den Fragetext.
- Format: eine Frage, ankreuzbare Optionen mit Konsequenzen *für den Menschen*, Vorschlag des Agenten mit Begründung, ohne Vorwissen beantwortbar.
- **Eskalationskriterium** (was überhaupt zum Menschen geht): nur was er *sieht*, was die App *in seinem Namen tut*, was *Geld kostet* oder *seine Rechner anfasst*. Test-Grenzwerte, Protokollformen, Codeaufbau entscheidet der Agent selbst und dokumentiert die Begründung.
- Antwort erhalten → sofort mit Datum + Wortlaut auf die Entscheidungskarte, Karte ins Archiv, abhängige Karten aktualisieren. Eine beantwortete Frage darf nirgendwo mehr als offen stehen.
- Antworten in Kartenkommentaren oder Chat-Nachrichten werden **auf die Karte übertragen** — Kommentare sind kein Speicherort für Entscheidungsstand.

## 7 · Agent-spezifische Regeln (Claude Code + Codex)

- **Ein Ticket pro Loop-Durchgang.** Fertig = gebaut, Tests grün, niedrigste echte Kante geprüft, Codex-Review — dann erst das nächste.
- **Max. 2 Codex-Runden je Ticket.** Alles, was Runde 2 zusätzlich findet und nicht das Ticket betrifft, wird eigene Karte. Sonst entstehen die 20-Schleifen-Loops, die dieses Regelwerk verhindern soll.
- **Bildbeleg:** Screenshots (auch Teilausschnitte, `Scripts/peek.sh`) gegen die vom Menschen angehängten Referenzbilder halten. Bewertung mit Codex an vier Fragen: Kern getroffen? Korrekt platziert? UI/UX? Design stimmig?
- **Nicht raten:** Versionen, SDKs, undokumentierte Protokolle → ermitteln, beobachten, dokumentieren, nachfragen.
- **Kontext-Hygiene:** Regeln/Architektur-Invarianten leben an genau einem Ort (Regelwerk-Karte + dieses Manual), nicht in wachsenden CLAUDE.md-Sammlungen (Board 65: CLAUDE.md wuchs auf 107 KB, bevor sie auf 11 KB + `docs/lessons.md` aufgeteilt wurde).
- Epic fertig + Claude und Codex zufrieden → Meldung an den Menschen (`say` o. ä.).

## 8 · Checkliste für Board-Reviews (wiederverwendbar)

1. Kodieren Spalten Workflow oder Historie/Themen? → Workflow erzwingen, Themen in Labels.
2. Wie viele erledigte Karten liegen in Arbeits-Spalten? → archivieren.
3. Gibt es Karten > 1 Seite Spezifikation? → Journal-Anteil nach `docs/`, ggf. splitten.
4. Steht irgendeine Entscheidung an mehr als einem Ort? → konsolidieren, eine Heimat-Karte.
5. Beantwortete Fragen, die noch als offen markiert sind? → Antworten eintragen, 🔴 entfernen.
6. Erfüllt jede Ready-Karte die Definition of Ready? → sonst zurück in Refinement/Entscheidungen/Blockiert.
7. Haben Blockiert-Karten einen benannten Blocker? 
8. Hat jedes Epic einen erreichbaren Fertig-Zustand? → sonst auflösen und verteilen.
9. Ist die physische Reihenfolge (order) gepflegt und bedeutet sie Priorität?
10. Doppelte Label-Zuweisungen / tote Labels? → bereinigen.