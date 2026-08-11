"""What this database means by a round, a ladder and a result.

Most of a football database needs no explanation. A handful of things do,
because the source made a decision that a reader would otherwise read as a
fault: a round numbered differently from the AFL's own fixture, a match the
AFL awards to the other side, a ladder ordered by something other than
points. Every one of those has produced the same question at least once,
and the answer belongs on the screen showing the number rather than in a
file nobody opens.

The notes are data, not prose in a page, for two reasons. A note is tied to
the seasons it applies to, so a 1991 season card can show the match-ratio
ladder rule without a 2011 card carrying it; and the same note has to
appear in several places -- the round card, the season card, the home
page -- without being written out three times and drifting.

Most of these are AFL Tables' own caveats and are reproduced because this
database inherits them wholesale. ROUND_NUMBERING is different: it is a
decision about how this database counts, and it is the one that reliably
confuses, because it makes our round numbers disagree with the AFL's from
2024 onward.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Note:
    """One caveat, and the seasons it applies to.

    `first` and `last` are inclusive and either may be None for an open
    end. A note with neither is true of the whole collection and is shown
    only in the full list -- attaching "some tables cut off at ten" to
    every season card would bury the notes that are actually about that
    season.
    """
    topic: str
    text: str
    first: int | None = None
    last: int | None = None

    @property
    def seasons(self) -> str:
        """`'1991–1994'`, `'2024 and beyond'`, `'Until 1989'`, or ''."""
        if self.first is None and self.last is None:
            return ""
        if self.first is None:
            return f"Until {self.last}"
        if self.last is None:
            return f"{self.first} and beyond"
        if self.first == self.last:
            return str(self.first)
        return f"{self.first}–{self.last}"

    def covers(self, season) -> bool:
        if self.first is None and self.last is None:
            return False
        try:
            season = int(season)
        except (TypeError, ValueError):
            return False
        return ((self.first is None or season >= self.first)
                and (self.last is None or season <= self.last))


#: The one note that is about this database rather than about football.
#:
#: From 2024 the AFL's fixture opens with a Round 0, branded Opening
#: Round. The source this database is built from declines to number a round
#: zero and calls that first round 1, so every later round is one higher
#: here than on the AFL's own fixture, and a season has twenty-five
#: home-and-away rounds where the AFL counts twenty-four. Renumbering would
#: mean rebuilding the database, and the numbering is at least internally
#: consistent, so it is documented instead.
ROUND_NUMBERING = Note(
    "Rounds",
    "The official AFL fixture opens with Round 0, branded Opening Round. "
    "A round numbered zero carries no meaning here, so this database "
    "ignores it and starts the season at Round 1. Every round from then on "
    "is therefore one higher than the AFL's official number — a season has "
    "25 home-and-away rounds here where the AFL counts 24.",
    first=2024,
)

NOTES: tuple[Note, ...] = (
    ROUND_NUMBERING,
    Note("Rounds",
         "One-off matches were played before Round 1. Their results are "
         "included in the Round 1 and every subsequent ladder.",
         first=1979, last=1979),
    Note("Rounds",
         "One-off matches were played before Round 1. Their results are "
         "included in the Round 1 and every subsequent ladder.",
         first=1982, last=1982),
    Note("Rounds",
         "One-off matches were played before Round 1. Their results are "
         "included in the Round 1 and every subsequent ladder.",
         first=1985, last=1985),
    Note("Rounds",
         "Rounds 16 and 17 were played over three weeks. The Round 16 "
         "ladder is derived from midway through the second week.",
         first=1987, last=1987),

    Note("Naming",
         "The Australian Football League was known as the Victorian "
         "Football League until 1989, which is why records are often "
         "labelled VFL/AFL. Everything here assumes that tag.",
         last=1989),

    Note("Ladders",
         "In seasons that used a bye without awarding competition points "
         "for it, a team with the bye in Round 1 would sit last on the "
         "ladder — below every team that lost — on zero points and zero "
         "percent. Those teams are given a nominal 100 percent for the "
         "Round 1 ladder instead."),
    Note("Ladders",
         "The competition had 15 teams, so byes were required. A bye won "
         "no competition points, and a ladder ranked on points alone would "
         "have favoured whoever had had fewer byes, so the ladder is "
         "ordered by match ratio: actual competition points × 100 ÷ "
         "possible competition points.",
         first=1991, last=1994),

    Note("Results",
         "Geelong v St Kilda at Corio Oval: St Kilda won the match but "
         "forfeited the premiership points to Geelong for playing a "
         "suspended player. It is counted as a St Kilda victory throughout "
         "this collection, which differs from the official AFL position "
         "that it was a Geelong win.",
         first=1909, last=1909),
    Note("Results",
         "Bob Rush performed grand final coaching duties while Jock McHale "
         "was ill. The AFL credits the absent McHale as coach rather than "
         "Rush, and so does this database.",
         first=1930, last=1930),
    Note("Results",
         "Essendon v St Kilda at Waverley Park: with 4m 48s of the third "
         "quarter left the ground lighting failed and could not be "
         "restored. The rest of the match was played the following "
         "Tuesday with newly selected teams, so 26 players turned out for "
         "Essendon and 25 for St Kilda. The 17,590 who attended the second "
         "phase are not included in the attendance figure.",
         first=1996, last=1996),
    Note("Results",
         "The Brisbane Lions are treated as a new club, statistically "
         "distinct from the Brisbane Bears and Fitzroy that founded them.",
         first=1997, last=1997),
    Note("Results",
         "St Kilda v Fremantle at York Park finished 94–94, St Kilda "
         "having kicked the levelling behind after the siren. That behind "
         "was disallowed on appeal and the result changed to a 94–93 "
         "Fremantle win, which is the result recorded here.",
         first=2006, last=2006),
    Note("Ladders",
         "Essendon were relegated to ninth place before Round 23. Their "
         "results and competition points remain intact.",
         first=2013, last=2013),
    Note("Ladders",
         "Adelaide v Geelong in Round 14 was cancelled after the death of "
         "Adelaide coach Phil Walsh on the preceding Friday. Each club "
         "received two competition points.",
         first=2015, last=2015),

    Note("Scope",
         "Some tables cut off at a fixed number of rows — a top ten, say. "
         "Eleventh place may hold the same tally as tenth."),
)


def for_season(season) -> list[Note]:
    """Every note that applies to one season, most recent rule first."""
    return [note for note in NOTES if note.covers(season)]


def round_numbering_note(season) -> Note | None:
    """The Opening Round caveat, when the season is one it applies to."""
    return ROUND_NUMBERING if ROUND_NUMBERING.covers(season) else None


def official_round(round_value, season) -> str | None:
    """What the AFL's own fixture calls this round, when it differs.

    Round 23 here is the AFL's Round 22 from 2024 on, because the AFL
    counts an Opening Round this database does not -- and our Round 1 is
    that Opening Round itself, which is worth naming rather than calling
    it Round 0. Returns a complete label, or None where the two fixtures
    agree or the round is not a number at all: a grand final is a grand
    final under either.
    """
    if not ROUND_NUMBERING.covers(season):
        return None
    text = str(round_value).strip().upper()
    if text.startswith("R") and text[1:].isdigit():
        text = text[1:]
    if not text.isdigit():
        return None
    number = int(text)
    if number == 1:
        return "Opening Round"
    return f"Round {number - 1}" if number > 1 else None


def by_topic() -> dict[str, list[Note]]:
    """Every note, grouped for the full list, in the order topics matter."""
    order = ["Rounds", "Ladders", "Results", "Naming", "Scope"]
    grouped: dict[str, list[Note]] = {topic: [] for topic in order}
    for note in NOTES:
        grouped.setdefault(note.topic, []).append(note)
    return {topic: items for topic, items in grouped.items() if items}
