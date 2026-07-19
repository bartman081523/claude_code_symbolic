# Aufgabe: Trapping Rain Water

Implementiere das Modul `solution.py` mit exakt dieser Funktion:

    solve(heights: list[int]) -> int

Zurückgegeben wird die Gesamtmenge (in Höheneinheiten) des Regenwassers, das
zwischen den Balken eingeschlossen bleibt, gegeben eine Liste nicht-negativer
ganzer Zahlen (Balkenhöhe je Index). Wasser, das an beiden Enden abfließen
würde, wird nicht gezählt.

Das ist das klassische "Trapping Rain Water"-Problem. Lösung in O(n) Zeit,
idealerweise mit O(1) zusätzlichem Platz (Zwei-Zeiger-Technik) — jede korrekte
O(n)-Lösung besteht die Tests.

WICHTIG: Die exakte Funktionssignatur, die Import-Erwartungen und ALLE
Testfälle stehen in `test.py`. Du MUSS zuerst `test.py` lesen, um das Interface
zu bestätigen, bevor du Code schreibst. Verändere `test.py` NICHT.

Nach dem Schreiben von `solution.py` ausführen:  python3 test.py
Alle Tests müssen bestehen. Schlägt einer fehl, `solution.py` korrigieren und
`python3 test.py` erneut laufen lassen, bis alle bestehen.

Melde am Ende das Ergebnis von solve([0,1,0,2,1,0,1,3,2,1,2,1]) und gib an,
welche Werkzeuge du verwendet hast.
