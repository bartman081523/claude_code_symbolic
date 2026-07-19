# Aufgabe: Cherry Pickup (zwei Wege, Kirschen pflücken)

Implementiere `solution.py` mit exakt:

    solve(grid: list[list[int]]) -> int

`grid` ist n×n mit Werten 0 (leer), 1 (Kirsche) oder -1 (Dorn, unpassierbar).
Du startest bei (0,0), gehst zum (n-1,n-1) und dann ZURÜCK zu (0,0). Pro Schritt
gehtst du auf dem HINWEG nur nach rechts oder unten; auf dem RÜCKWEG nur nach
links oder oben. Du pflückst jede Kirsche (1) auf einem Feld, das du betrittst
(das Feld wird danach zu 0). Stehst du mit beiden Wegen auf demselben Feld, zählt
die Kirsche nur einmal. Du darfst keine -1-Felder betreten. Gib die maximale
Anzahl gepflückter Kirschen zurück. Falls (0,0) oder (n-1,n-1) ein Dorn ist oder
kein Weg existiert, gib 0 zurück.

Tipp zum Nachdenken: der Rückweg von (n-1,n-1) nach (0,0) mit links/oben ist
dasselbe wie ein zweiter Hinweg von (0,0) nach (n-1,n-1) mit rechts/unten. Betrachte
also ZWEI simultane Pfade von (0,0) nach (n-1,n-1).

Die exakte Signatur und alle Testfälle stehen in `test.py`. LIES zuerst `test.py`.
Verändere `test.py` NICHT. Schreibe `solution.py`, führe `python3 test.py` aus,
iteriere bis "ALL TESTS PASSED" oder bis du nicht weiterkommst. Melde am Ende
das Ergebnis von solve([[0,1,-1],[1,0,1],[1,1,1]]) und ob du bestanden hast (PASS/FAIL).
