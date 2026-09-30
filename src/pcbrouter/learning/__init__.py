"""Learning from routing outcomes (local, anonymised, never trusted for legality).

Level 1 (this package): an experience log. Every board-routing job appends one
record per net: board and net features, the settings used and the outcome.
Later levels choose *search settings* from it; the exact validator still decides
every piece of copper, so learning can change speed and completion, never legality.
"""
