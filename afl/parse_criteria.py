"""
afl/parse_criteria.py -- Turn Gridley's criterion text into constraints.

Gridley writes squares as short phrases: "150+ GAMES PLAYED",
"30+ GOALS TWO DIFF CLUBS", "MASON WOOD TEAMMATE". This maps those to
the constraint functions in afl/constraints.py.

Anything it can't map returns None with a reason, so an unrecognised
square is reported rather than silently answered wrong.
"""

import re
from . import constraints as C
from . import awards as A
from . import brownlow as B

# Gridley shows clubs as logos, not text. Column identity usually arrives
# as a club name or slug from the API; these are the aliases we accept.
CLUB_ALIASES = {
    "st kilda": "St Kilda", "saints": "St Kilda", "stkilda": "St Kilda",
    "north melbourne": "North Melbourne", "kangaroos": "North Melbourne",
    "north": "North Melbourne", "roos": "North Melbourne",
    "western bulldogs": "Western Bulldogs", "bulldogs": "Western Bulldogs",
    "footscray": "Footscray", "dogs": "Western Bulldogs",
    "brisbane lions": "Brisbane Lions", "lions": "Brisbane Lions",
    "brisbane bears": "Brisbane Bears", "bears": "Brisbane Bears",
    "greater western sydney": "GWS", "gws": "GWS", "giants": "GWS",
    "sydney": "Sydney", "swans": "Sydney", "south melbourne": "Sydney",
    "adelaide": "Adelaide", "crows": "Adelaide",
    "port adelaide": "Port Adelaide", "power": "Port Adelaide",
    "west coast": "West Coast", "eagles": "West Coast",
    "fremantle": "Fremantle", "dockers": "Fremantle",
    "gold coast": "Gold Coast", "suns": "Gold Coast",
    "carlton": "Carlton", "blues": "Carlton",
    "collingwood": "Collingwood", "magpies": "Collingwood", "pies": "Collingwood",
    "essendon": "Essendon", "bombers": "Essendon",
    "geelong": "Geelong", "cats": "Geelong",
    "hawthorn": "Hawthorn", "hawks": "Hawthorn",
    "melbourne": "Melbourne", "demons": "Melbourne", "dees": "Melbourne",
    "richmond": "Richmond", "tigers": "Richmond",
    "fitzroy": "Fitzroy", "university": "University",
}

# Criterion words -> the stat column they refer to.
STAT_WORDS = {
    "disposal": "disposals", "kick": "kicks", "handball": "handballs",
    "mark": "marks", "goal": "goals", "behind": "behinds",
    "tackle": "tackles", "hit out": "hitouts", "hitout": "hitouts",
    "inside 50": "inside50s", "clearance": "clearances",
    "rebound": "rebounds", "contested possession": "contested",
    "one percenter": "one_percenters", "bounce": "bounces",
    "goal assist": "goal_assists", "brownlow vote": "brownlow",
    "frees against": "frees_against", "free kicks against": "frees_against",
    "frees for": "frees_for", "free kicks for": "frees_for",
    "clanger": "clangers", "uncontested possession": "uncontested",
    # In AFL_STATS since the beginning but never given a criterion word, so
    # "5+ CONTESTED MARKS" matched the shorter "mark" key and silently
    # answered a question about total marks instead.
    "contested mark": "contested_marks",
    "mark inside 50": "marks_i50", "marks inside 50": "marks_i50",
}

# STAT_WORDS is matched by substring, and several keys are prefixes of
# others ("mark"/"contested mark", "frees for"/"frees against" once the
# leading word is shared). Iterating the dict in insertion order lets the
# shorter key win, which silently resolves "CONTESTED MARKS" to `marks`.
# Every substring loop below iterates this instead, longest key first.
STAT_WORDS_BY_LENGTH = sorted(STAT_WORDS, key=len, reverse=True)

# Wording a derby square can use, mapped to the keys match_constraints.DERBIES
# is written in. Longest alias first so "sydney derby" is not read as the
# bare "derby" of some other fixture.
DERBY_ALIASES = {
    "western derby": "western_derby",
    "sydney derby": "sydney_derby",
    "showdown": "showdown",
    "q clash": "q_clash",
    "q-clash": "q_clash",
    "qclash": "q_clash",
}

# Words that rule the "bare text is a player's name" last resort out. Every
# one of them belongs to a criterion rule above, so text carrying one is a
# criterion this parser could not read rather than somebody's name.
_NOT_A_NAME = (
    r"\b(?:game|match|season|career|final|club|draft|award|medal|star|pick|"
    r"win|winning|won|loss|lost|draw|tied|brownlow|premiership|flag|spoon|"
    r"record|first|last|debut|retire|year|age|team|player|stadium|oval|"
    r"park|ground|derby|showdown|clash|captain|played|footed|foot|"
    r"handed|born|nominee|coach|umpire|round|crowd)s?\b"
)

# Criteria the database genuinely cannot express.
UNSUPPORTED = {
    # Retained as a fallback only. Family wording with a trusted mapping
    # is handled by _parse_family_criterion before this dict is consulted.
    r"\bbrother|\bfather\b|\bson\b|related": "no trusted family link for that wording",
    r"\b(born|from) (tas|vic|wa|sa|nsw|qld|nt)": "birthplace isn't in the data",
    r"tasmanian|indigenous|irish|father[- ]son|academy":
        "player background isn't in the available linked data",
    r"guernsey|jumper number|number \d+": "jumper numbers aren't stored",
    r"\bcoach": "coaching records aren't in the players table",
    # Wordings Gridley has actually used whose data no loaded layer holds.
    # Each declines by name, so the board can say what is missing instead
    # of shrugging "couldn't interpret".
    r"22\s*under\s*22": "the AFLPA 22Under22 team isn't in the linked award data",
    r"\b(?:mark|goal) of the year\b":
        "Mark and Goal of the Year winners aren't in the linked award data",
    r"\blisted player\b|\bcurrently listed\b":
        "club lists aren't in the data — only players with a recorded game",
    r"int'?l rules|international rules":
        "International Rules representation isn't in the data",
    r"after (?:the )?siren": "shot-by-shot match timing isn't recorded",
    r"\brecruited by\b": "recruiter attribution isn't recorded",
    r"\bspoils?\b": "spoils aren't recorded in AFL Tables data",
    r"\bfirst name\b": "first-name novelty squares aren't supported",
    r"\bnfl\b": "other-league careers aren't in the data",
}


def _num(s, default=None):
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else default


# Phrases that mean "at most n" rather than "fewer than n". Gridley writes
# both, and the difference is a whole boundary player. The physical forms
# matter as much as the counting ones: "180cm OR SHORTER" includes the
# 180cm player, and reading it strictly dropped everyone on the boundary.
_INCLUSIVE_MAX = re.compile(r"or fewer|or less|at most|no more than|up to"
                            r"|or shorter|or lighter|or under|or smaller")
_STRICT_MAX = re.compile(r"less than|fewer than|under|below|\bunder\b|<")


def _is_max(t):
    """True if the criterion caps a total rather than setting a floor."""
    return bool(_STRICT_MAX.search(t) or _INCLUSIVE_MAX.search(t))


def _max_bound(t, n):
    """
    Translate a capped phrase into the inclusive bound the SQL wants.

    "LESS THAN 20 GOALS" is 19, not 20. The builders take an inclusive
    `<=`, so the strict wording has to lose one here, where the words are
    still in front of us -- doing it inside the builder would leave two
    near-identical functions chosen by guesswork.
    """
    if _INCLUSIVE_MAX.search(t):
        return n
    return max(n - 1, 0)


def _season_window(t):
    """A span of seasons named in the text, or None.

    Three shapes Gridley writes: a decade ("DURING 2020s"), an explicit
    range ("2010 TO 2019"), and an open end ("2020 ONWARDS"). The bare
    four-digit year is deliberately not one of them -- "2004 ALL
    AUSTRALIAN" is a single season and stays with the rules that own it.
    """
    m = re.search(r"\b(18|19|20)(\d)0s\b", t)
    if m:
        lo = int(f"{m.group(1)}{m.group(2)}0")
        return lo, lo + 9
    m = re.search(r"\b((?:18|19|20)\d{2})\s*(?:to|[-–])\s*"
                  r"((?:18|19|20)\d{2})\b", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b((?:18|19|20)\d{2})\s*(?:onwards?|or later|and later)\b",
                  t)
    if m:
        return int(m.group(1)), 9999
    return None


def _person_name(words):
    """Title-case a name, undoing the two ways Gridley's display mangles it.

    The two-line board often repeats the surname ("MAX GAWN / GAWN
    TEAMMATE" arrives glued as "MAX GAWN GAWN TEAMMATE"), so a final word
    already present earlier truncates the run at its first appearance.
    title() then lowercases the C in "McKercher"; put it back.
    """
    words = list(words)
    if len(words) > 2 and words[-1] in words[:-1]:
        words = words[: words.index(words[-1]) + 1]
    name = " ".join(words).title()
    return re.sub(r"\bMc([a-z])", lambda m: f"Mc{m.group(1).upper()}", name)


def _family_builders_available():
    """True when afl/constraints.py exposes the broad family relationship layer."""
    return all(
        hasattr(C, name)
        for name in (
            "family_member",
            "sibling_also_played",
            "brother_also_played",
            "parent_or_child_also_played",
            "father_or_son_also_played",
            "extended_family_also_played",
            "relative_played_for",
        )
    )


def _parse_family_criterion(t):
    """
    Map a broad family square onto the Wikipedia family relationship layer.

    Returns (constraint, label), or None so wording with no trusted mapping
    still falls through to the UNSUPPORTED decline instead of guessing.
    Only trusted (uniquely or confidently linked) relationships are matched
    by the builders, so a square answers conservatively or not at all.
    """
    if not _family_builders_available():
        return None

    # "RELATIVE PLAYED FOR GEELONG" / "BROTHER PLAYED AT CARLTON"
    if re.search(r"\bplayed (?:for|at|with)\b|\bat\b", t):
        for alias, club in sorted(
                CLUB_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
            if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", t):
                return (C.relative_played_for(club),
                        f"relative played for {club}")

    if re.search(r"\bbrothers?\b", t):
        return C.brother_also_played(), "brother also played"
    if re.search(r"\bsiblings?\b|\bsisters?\b|\btwins?\b", t):
        return C.sibling_also_played(), "sibling also played"
    if re.search(r"\bfathers?\b|\bdad\b|\bsons?\b", t):
        return C.father_or_son_also_played(), "father/son also played"
    if re.search(r"\bparents?\b|\bmothers?\b|\bmum\b|\bdaughters?\b|"
                 r"\bchild(?:ren)?\b", t):
        return C.parent_or_child_also_played(), "parent/child also played"
    if re.search(r"\bgrand(?:father|mother|son|daughter|parent|child)\b|"
                 r"\bcousins?\b|\buncles?\b|\baunts?\b|\bnephews?\b|"
                 r"\bnieces?\b|\bin[- ]laws?\b", t):
        return C.extended_family_also_played(), "extended family also played"
    if re.search(r"\brelatives?\b|\bfamily\b", t):
        return C.family_member(), "AFL/VFL family member"
    return None


def _parse_exact(text):
    """
    Return (constraint, label) or (None, reason).
    `text` is one Gridley criterion, e.g. "30+ GOALS TWO DIFF CLUBS".
    """
    if not text:
        return None, "empty criterion"

    t = " ".join(str(text).lower().split())
    t = t.replace("’", "'").replace("+", "+ ")
    # League-name noise: "100+ VFL/AFL GAMES" and "100+ GAMES" are the
    # same question, and every rule below should see them the same way.
    t = re.sub(r"\bvfl\s*/\s*afl\b|\bafl\s*/\s*vfl\b|\bvfl\b"
               r"|\bafl\b(?!\s*tables)", " ", t)
    # "KICKED 30+ GOALS" is the scoring verb, not the kicks statistic --
    # the stat table's "kick" sits inside "kicked" and was winning the
    # word race, answering a goals criterion with kicks. The kicks stat
    # is always phrased as the noun ("20+ KICKS"), never past tense.
    t = re.sub(r"\bkicked\b", " ", t)
    t = " ".join(t.split())

    # 1. A bare club name.
    stripped = t.replace("+", "").strip()
    if stripped in CLUB_ALIASES:
        club = CLUB_ALIASES[stripped]
        return C.played_for(club), club

    # 1b. Teammate criteria, claimed before anything keyed on a substring.
    # Surnames collide with award names -- "KEIDEAN COLEMAN TEAMMATE" is
    # not the Coleman Medal and "NICK LARKEY TEAMMATE" is not the Larke --
    # so text that says teammate IS a teammate square, whatever else its
    # letters happen to contain. The count form is claimed first so
    # "100+ TEAMMATES CAREER" cannot read as somebody named Career.
    m = re.search(r"(\d+)\s*\+?\s*teammates\b", t)
    if m:
        return (C.career_teammates_min(int(m.group(1))),
                f"{m.group(1)}+ career teammates")
    m = re.match(r"^(.+?)\s+teammates?(?:\s+of\s+\S+)?$", t)
    if m and re.fullmatch(r"[a-z][a-z\-' ]*", m.group(1)):
        name = _person_name(m.group(1).split())
        return C.teammate_of(name), f"{name} teammate"

    # 2. Awards and Draftguru signing types. These must run before the
    # unsupported checks so "All-Australian captain" is not mistaken for
    # unsupported club captaincy, and explicit father-son selections are
    # not swallowed by the broader family-link decline.
    if re.search(r"all[- ]australian", t):
        if "captain" in t:
            return A.all_australian_captain(), "All-Australian captain"
        if "squad" in t:
            return A.all_australian_squad(), "All-Australian squad"
        # "ALL AUSTRALIAN FORWARD": one line of the ground, not any team
        # spot. Named lines only -- Gridley excludes the interchange from
        # these squares, and so does the builder's own grouping.
        position = next((group for word, group in (
            ("forward", "forward"), ("defender", "back"),
            ("defence", "back"), ("back", "back"),
            ("midfield", "midfield"), ("centre", "midfield"),
            ("wing", "midfield"), ("ruck", "ruck"),
            ("interchange", "interchange"), ("bench", "interchange"),
        ) if re.search(rf"\b{word}", t)), None)
        if position:
            return (A.all_australian_position_group(position),
                    f"All-Australian {position}")
        window = _season_window(t)
        if window:
            lo, hi = window
            label = (f"All-Australian {lo}-{hi}" if hi < 9999
                     else f"All-Australian since {lo}")
            return A.all_australian_between(lo, hi), label
        one_year = re.search(r"\b((?:19|20)\d{2})\b", t)
        if one_year:
            year = int(one_year.group(1))
            return A.all_australian_between(year, year), f"{year} All-Australian"
        times = re.search(r"(\d+)\s*(?:x|times?)\b", t)
        n = int(times.group(1)) if times else None
        if n and n > 1:
            return A.all_australian(n), f"{n}x All-Australian"
        return A.all_australian(1), "All-Australian"

    # Full Brownlow voting results. These precede the winners-only award rule
    # so "top 5 Brownlow finish" cannot collapse to "won a Brownlow".
    m = re.search(r"(\d+)\s*(?:x|times?)\b.{0,16}\btop\s*(\d+)"
                  r".{0,20}\bbrownlow", t)
    if m:
        times, place = int(m.group(1)), int(m.group(2))
        return (B.brownlow_top_finish_times(place, times),
                f"top-{place} Brownlow finish {times}+ times")

    m = re.search(r"(?:top\s*(\d+).{0,20}\bbrownlow|"
                  r"\bbrownlow.{0,20}\btop\s*(\d+))", t)
    if m:
        place = int(m.group(1) or m.group(2))
        return B.brownlow_top_finish(place), f"top-{place} Brownlow finish"

    if re.search(r"\bbrownlow\b.{0,12}\bpodium\b|\bpodium\b.{0,12}\bbrownlow", t):
        return B.brownlow_top_finish(3), "top-3 Brownlow finish"

    if re.search(r"\bbrownlow\b.{0,12}\brunner[- ]?up\b|"
                 r"\brunner[- ]?up\b.{0,12}\bbrownlow", t):
        return B.brownlow_exact_finish(2), "Brownlow runner-up"

    m = re.search(r"(?:finish(?:ed)?\s*|finish\s+of\s+)(\d+)"
                  r"(?:st|nd|rd|th)?\s*(?:in|for)?\s*(?:the\s+)?brownlow", t)
    if not m:
        m = re.search(r"brownlow\s+finish\s*(?:of\s+)?(\d+)"
                      r"(?:st|nd|rd|th)?", t)
    if m:
        place = int(m.group(1))
        return B.brownlow_exact_finish(place), f"finished {place} in the Brownlow"

    m = re.search(r"(\d+)\+?\s*brownlow votes?.{0,16}\b(?:a|one) season\b", t)
    if m:
        votes = int(m.group(1))
        return B.brownlow_votes_in_season(votes), f"{votes}+ brownlow votes in a season"

    m = re.search(r"(?:won|winning)\s+(?:the\s+|a\s+)?brownlow.{0,16}?"
                  r"(\d+)\s*\+?\s*votes"
                  r"|brownlow.{0,10}\bwith\s+(\d+)\s*\+?\s*votes", t)
    if m:
        votes = int(m.group(1) or m.group(2))
        return (B.brownlow_won_with_votes(votes),
                f"won the Brownlow with {votes}+ votes")

    if re.search(r"brownlow (medal(?:list|ist)?|winner)", t):
        return A.brownlow_medallist(), "Brownlow Medallist"
    if re.search(r"coleman", t):
        return A.coleman_medallist(), "Coleman Medallist"
    if re.search(r"norm smith", t):
        return A.norm_smith_medallist(), "Norm Smith Medallist"
    if re.search(r"rising star (winner|medallist)", t):
        return A.won_award("rising-star"), "Rising Star winner"

    state_awards = {
        "magarey": "magarey-medal",
        "sandover": "sandover-medal",
        "liston": "liston-trophy",
        "morrish": "morrish-medal",
        "larke": "larke-medal",
        "hunter harrison": "hunter-harrison-medal",
        "gardiner": "gardiner-medal",
        "geoff christian": "geoff-christian-medal",
    }
    for phrase, slug in state_awards.items():
        if phrase in t:
            return A.won_award(slug), f"{phrase.title()} medallist"
    if re.search(r"state[- ]league medallist", t):
        return A.state_league_medallist(), "state-league medallist"

    if re.search(r"hall of fame", t):
        return C.hall_of_fame_player(), "Hall of Fame player"

    # Gridley writes the club award as "BEST & FAIREST", with the ampersand
    # its own word. Only "best and fairest" and "b&f" were matched here, so
    # the one spelling the site actually uses fell through every rule below
    # and was declined as uninterpretable -- on a board whose other five
    # criteria parsed, which made the whole grid unplayable.
    if re.search(r"best\s*(?:and|&|'?n'?)\s*fairest|\bb\s*&\s*f\b", t):
        m = re.search(r"(\d+)\+?\s*(?:different\s+)?clubs?", t)
        if m:
            n = int(m.group(1))
            return A.best_and_fairest_at_multiple_clubs(n), f"B&F at {n}+ clubs"
        for alias, club in CLUB_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", t):
                return A.best_and_fairest_at(club), f"{club} best and fairest"
        n = _num(t)
        if n and n > 1:
            # "2X BEST & FAIREST" already asked for two, and the axis has to
            # say so: the label is the only place the board states which
            # question was answered.
            return A.best_and_fairest(n), f"{n}x club best and fairest"
        return A.best_and_fairest(1), "club best and fairest"

    if re.search(r"(number|pick) ?(one|1)\b.*(draft|pick)|#1 (draft )?pick", t):
        return A.number_one_draft_pick(), "number one draft pick"
    
    # "TOP 10 PICK", and the bare "TOP 10" Gridley writes for the same
    # square (grid 1117). Anything *else* in the text means the number
    # belongs to that instead: "TOP 10 GOALKICKER" is not a draft criterion,
    # and matching a bare "\btop\s*(\d+)" answered it as one. The awards
    # block above has already claimed "TOP 10 BROWNLOW FINISH".
    m = re.search(r"\btop\s*(\d+)\b", t)
    if m and (re.fullmatch(r"top\s*\d+", t.strip())
              or re.search(r"\bdraft(ed|ee)?\b|\bpicks?\b|\bselections?\b", t)):
        n = int(m.group(1))
        return C.draft_pick_between(1, n), f"top {n} draft pick"
    if re.search(r"\bfather[- ]son(?: selection)?\b", t):
        return A.father_son(), "father-son selection"
    if re.search(r"\bacademy selection\b", t):
        return A.academy_selection(), "academy selection"

    # "ROOKIE DRAFT PICK": a named draft category rather than a pick number.
    if re.search(r"\bdraft|\bpick|\bselection", t):
        for word, kind in (("rookie", "Rookie"), ("pre-season", "Pre-Season"),
                           ("pre season", "Pre-Season"),
                           ("mid-season", "Mid-Season"),
                           ("mid season", "Mid-Season")):
            if word in t:
                return C.draft_of_type(kind), f"{kind} draft selection"

    # "TRADED 1+ TIMES": the Draftguru trade records, from 1988.
    m = re.search(r"\btraded\b(?:\s*(\d+)\s*\+?\s*times?)?", t)
    if m:
        times = int(m.group(1)) if m.group(1) else 1
        return (C.traded_min(times),
                f"traded {times}+ times" if times > 1 else "traded at least once")

    # "RECRUITED FROM GLENELG", "FROM OAKLEIGH CHARGERS", "VIA NORWOOD".
    # The rest of the text is the place, so this runs after every rule
    # that owns a keyword -- a criterion naming a club and a statistic is
    # not a recruitment square. Matching is anchored to a step of the
    # path, so "Oakleigh" reaches the Oakleigh talent-league club without
    # "Geelong" quietly meaning three different places.
    m = re.match(r"^(?:recruited |drafted |came )?(?:from|via)\s+(.{3,})$", t)
    if m:
        source = m.group(1).strip().strip(".")
        # Draftguru writes the talent-league clubs as "Oakleigh U18", not
        # by their brand name, and a reader writes the brand name.
        source = re.sub(r"\s+(chargers|falcons|stingrays|cannons|dragons|"
                        r"jets|rebels|power|knights|ranges|bushrangers|"
                        r"pioneers|eagles|calder|talent league|u18s?)$",
                        "", source).strip()
        if source and not any(word in source for word in STAT_WORDS):
            return C.recruited_from(source.title()), f"recruited from {source}"

    # 3. Explicitly unsupported.
    # Parse optional linked club-captain criteria.
    # All-Australian captain is parsed earlier by the awards block.
    if re.search(r"\bclub captain\b|^captain$|\bwas (?:a )?captain\b|"
                 r"\bcaptained(?: of| for)?\b", t):
        aliases = globals().get(
            "CLUB_ALIASES", {club.lower(): club for club in C.CLUBS})
        for alias, club in sorted(
                aliases.items(), key=lambda item: len(item[0]), reverse=True):
            if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", t):
                return C.captain_of(club), f"{club} captain"
        return C.club_captain(), "club captain"

    # SDL_RISING_STAR_PARSE — optional FootyWire nomination layer. A bare
    # "RISING STAR" axis means the nomination: Gridley words the award as
    # "RISING STAR WINNER", which the awards block above already claimed,
    # so the nominee wording is optional here (board #1117 says only
    # "RISING STAR").
    if (re.search(r"\brising star\b", t)
            and not re.search(r"\b(winner|won|medallist)\b", t)):
        years = [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", t)]
        aliases = globals().get(
            "CLUB_ALIASES", {club.lower(): club for club in C.CLUBS})
        club = None
        for alias, canonical in sorted(
                aliases.items(), key=lambda item: len(item[0]), reverse=True):
            if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", t):
                club = canonical
                break
        if club and len(years) >= 2:
            return (C.rising_star_nominee_for_between(club, years[0], years[1]),
                    f"{club} Rising Star nominee {years[0]}–{years[1]}")
        if club:
            return C.rising_star_nominee_for(club), f"{club} Rising Star nominee"
        if len(years) >= 2:
            return (C.rising_star_nominee_between(years[0], years[1]),
                    f"Rising Star nominee {years[0]}–{years[1]}")
        if len(years) == 1:
            return C.rising_star_nominee_in(years[0]), f"{years[0]} Rising Star nominee"
        return C.rising_star_nominee(), "Rising Star nominee"

    # SDL_FAMILY_PARSE -- optional broad Wikipedia family layer. Runs before
    # the UNSUPPORTED decline so "BROTHER PLAYED" resolves to real answers,
    # and after the awards block so father-son *selections* stay a draft
    # criterion rather than a relationship one.
    if re.search(r"\bbrother|\bsibling|\bsister|\btwin|\bfather|\bdad\b|"
                 r"\bmother|\bmum\b|\bparent|\bson\b|\bsons\b|"
                 r"\bdaughter|\bchild|\bgrand|\bcousin|\buncle|\baunt|"
                 r"\bnephew|\bniece|\bin[- ]law|\brelative|\bfamily", t):
        family_hit = _parse_family_criterion(t)
        if family_hit is not None:
            return family_hit

    for pat, why in UNSUPPORTED.items():
        if re.search(pat, t):
            return None, why

    # 4. Teammate of a named player.
    m = re.match(r"^(.+?)\s+teammate$", t)
    if m:
        name = m.group(1).strip().title()
        return C.teammate_of(name), f"{name} teammate"

    # 3b. Venue squares. "MCG WON A FINAL" must beat the generic rules.
    # "PLAYED IN CHINA" first: the square means the Shanghai fixtures, and
    # the ground has a name the database knows.
    if re.search(r"\bchina\b", t):
        return (C.played_at_venue("Jiangwan Stadium"),
                "played at Jiangwan Stadium (China)")
    venue_hit = None
    for alias in sorted(C.VENUE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", t):
            venue_hit = alias
            break
    if venue_hit:
        canon = C.VENUE_ALIASES[venue_hit]
        if re.search(r"won a final|final win", t):
            return C.won_a_final_at(canon), f"won a final at {venue_hit}"
        # "100+ GAMES AT THE MCG" is a tenure count, and the bare
        # played-at fallback below used to answer it as "ever appeared
        # there" -- confidently wrong by a hundred games. Gridley words
        # the same square "PLAYED AT MCG 100+ Times", so the count nouns
        # include "times" and "appearances", not only "games".
        m = re.search(r"(\d+)\s*\+?\s*(?:games?|matches|times|appearances)\b",
                      t)
        if m:
            count = int(m.group(1))
            return (C.games_at_venue_min(canon, count),
                    f"{count}+ games at {venue_hit}")
        # "MCG KICKED A GOAL": a single-game feat at the ground. The verb
        # "kicked" was stripped with the league noise, so the stat word
        # and an optional number are what remains.
        stat_word = next((w for w in STAT_WORDS_BY_LENGTH if w in t), None)
        if stat_word:
            col = STAT_WORDS[stat_word]
            n = _num(t, 1)
            try:
                built = C.venue_stat_in_game(canon, col, n)
            except ValueError:
                built = None
            if built is not None:
                return built, f"{n}+ {col} in a game at {venue_hit}"
        return C.played_at_venue(canon), f"played at {venue_hit}"

    # 3c. "<CLUB> FIRST CAREER GAME" / "DEBUTED FOR <CLUB>"
    if re.search(r"first (career )?game|debut", t):
        for alias, club in CLUB_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", t):
                return C.debut_club(club), f"{club} first career game"
        # "DEBUT GAME 2010 TO 2019" / "DEBUT GAME 2020 ONWARDS": a first
        # game inside a window is debuted_between, not played_in_range.
        window = _season_window(t)
        if window:
            lo, hi = window
            return (C.debuted_between(lo, hi),
                    f"debuted {lo}-{hi}" if hi < 9999
                    else f"debuted {lo} onwards")

    # 3c9. The minor premiership: finishing top of the home-and-away ladder.
    # It is not the premiership and usually not even the same club, so it
    # has to be settled before every rule below that matches a bare
    # "premiership" -- one of which answered "MINOR PREMIERSHIP WINNER" with
    # the flag, and "NEVER WON A MINOR PREMIERSHIP" with its exact opposite.
    if re.search(r"minor premiership|minor flag|top of the ladder|"
                 r"ladder leader", t):
        if re.search(r"\bno\b|\bnever\b|\bwithout\b|\bzero\b", t):
            return C.no_minor_premierships(), "no minor premierships"
        m = re.search(r"(\d+)\+?\s*minor premierships?", t)
        if m and int(m.group(1)) > 1:
            count = int(m.group(1))
            return (C.minor_premierships_min(count),
                    f"{count}+ minor premierships")
        return C.minor_premiership_player(), "minor premiership"

    # 3d-pre. Rounds and shapes the generic finals rules must not swallow.
    # A Preliminary Final is its own round: answering "PRELIM FINAL PLAYER"
    # with any finals appearance is wrong by two whole weeks of September.
    if re.search(r"prelim(?:inary)?\s*finals?", t):
        n = _num(t, 1)
        return (C.preliminary_finals_min(n),
                f"played {n}+ preliminary finals" if n > 1
                else "played a preliminary final")

    # "5+ FINALS WINS" counts victories, where the bare rule below answers
    # any single one.
    m = re.search(r"(\d+)\s*\+?\s*finals? wins?\b", t)
    if m:
        return (C.finals_wins_min(int(m.group(1))),
                f"{m.group(1)}+ finals wins")

    # "GRAND FINAL FOR TWO CLUBS" before the plainer finals version of the
    # same shape, which would otherwise claim it.
    m = re.search(r"grand finals?\b.{0,16}\b(?:multiple|two|three|2|3)\+?"
                  r"\s*clubs", t)
    if m:
        k = 3 if re.search(r"\b(?:three|3)\b", t) else 2
        return (C.grand_final_at_multiple_clubs(k),
                f"grand final for {k}+ clubs")

    # "FINALS PLAYER MULTIPLE CLUBS" -- September football for 2+ clubs.
    if re.search(r"finals?\b.{0,20}\b(?:for )?(?:multiple|two|three|2|3)\+?"
                 r"\s*clubs", t):
        k = 3 if re.search(r"\b(?:three|3)\b", t) else 2
        return (C.finals_at_multiple_clubs(k),
                f"played finals for {k}+ clubs")

    # 3d. Finals counts and averages.
    m = re.search(r"(\d+)\+?\s*finals? games?", t)
    if m:
        return C.finals_games_min(int(m.group(1))), f"{m.group(1)}+ finals games"
    m = re.search(r"(\d+)\+?\s*grand finals?", t)
    if m:
        # "LOST 2+ GRAND FINALS" and "WON 2+ GRAND FINALS" both count grand
        # finals, and answering either with the bare "played in" count is
        # wrong in opposite directions -- it let every dual premiership
        # player through a square asking who had lost two.
        count = int(m.group(1))
        if re.search(r"\blost\b|\blose\b|\blosing\b|\bloss(es)?\b|"
                     r"\brunners?[- ]up\b|\bdefeated in\b", t):
            return (C.grand_finals_lost_min(count),
                    f"lost {count}+ grand finals")
        if re.search(r"\bwon\b|\bwin\b|\bwinning\b|\bwinner\b|"
                     r"\bpremierships?\b|\bflags?\b", t):
            return (C.premierships_won_min(count),
                    f"won {count}+ premierships")
        return (C.grand_finals_played_min(count),
                f"played in {count}+ grand finals")

    # "DEFEATED BY DUSTY IN A GF": the losing side against a named player.
    m = re.match(r"^(.+?)\s+defeated by\b.*\b(?:gf|grand final)", t)
    if m and re.fullmatch(r"[a-z][a-z\-' ]*", m.group(1)):
        name = _person_name(m.group(1).split())
        return (C.lost_grand_final_to(name),
                f"lost a grand final to {name}")

    # "LOST A GRAND FINAL" with no number is still about losing one, and
    # the participation rule below used to swallow it -- a square about
    # defeat answered with everyone who was merely there.
    if (re.search(r"\b(?:lost|losing|lose)\b.{0,14}grand final", t)
            and not re.search(r"\bnever\b|\bno\b", t)):
        return C.grand_finals_lost_min(1), "lost a grand final"

    # "MULTI-PREMIERSHIP PLAYER" and "3x PREMIERSHIP PLAYER" count flags;
    # the bare premiership rule in section 4 answers one, which accepted
    # every single-flag player for a square that asked for the repeat.
    if re.search(r"multi[- ]premiership", t):
        return C.premierships_won_min(2), "won 2+ premierships"
    m = re.search(r"(\d+)\s*x\s*premiership", t)
    if m and int(m.group(1)) > 1:
        n = int(m.group(1))
        return C.premierships_won_min(n), f"won {n}+ premierships"

    # Era-scoped flags and Grand Finals: "PREMIERSHIP PLAYER 2010 TO 2019",
    # "GRAND FINAL PLAYER DURING 2020s". Unscoped rules would accept any
    # era's premiership for a square that names one.
    window = _season_window(t)
    if window and re.search(r"premiership|flag|(?:won|win|winner).{0,12}"
                            r"grand final", t):
        lo, hi = window
        return (C.premiership_between(lo, hi),
                f"premiership {lo}-{hi}" if hi < 9999
                else f"premiership since {lo}")
    if window and re.search(r"grand final", t):
        lo, hi = window
        return (C.grand_final_between(lo, hi),
                f"grand final {lo}-{hi}" if hi < 9999
                else f"grand final since {lo}")

    m = re.search(r"([\d.]+)\+?\s*goals?\s*(avg|average)", t)
    if m and "final" in t:
        return (C.goal_average_in_finals(float(m.group(1))),
                f"{m.group(1)}+ goal avg in finals")
    if re.search(r"no grand final", t):
        return C.no_grand_finals(), "no grand finals"
    # Winning a grand final is a premiership, and must be tested before the
    # bare "grand final" rule below, which would otherwise answer the
    # weaker "played in one" for a criterion that says "won".
    if re.search(r"(won|win|winner).{0,12}grand final|grand final (win|winner)",
                 t):
        return C.premiership_player(), "premiership player"
    # "3+ GOALS IN A GRAND FINAL" is a feat on the day, and the bare
    # participation rule below used to swallow it -- a stat criterion
    # answered as merely having been there.
    gf_stat = (None if _is_max(t)
               else next((w for w in STAT_WORDS_BY_LENGTH if w in t), None))
    if gf_stat and "grand final" in t:
        m = re.search(r"([\d.]+)\s*\+?", t)
        if m:
            raw = float(m.group(1))
            n = int(raw) if raw.is_integer() else raw
            col = STAT_WORDS[gf_stat]
            return (C.stat_in_a_grand_final(col, n),
                    f"{n}+ {col} in a grand final")
    if re.search(r"played (in )?a grand final|grand final", t):
        return C.played_a_grand_final(), "played a grand final"
    # "WON A FINALS GAME" -- a finals win anywhere, as opposed to
    # won_a_final_at(), which the venue branch above has already claimed.
    # The negation guard matters: "NO FINALS WINS" and "NEVER WON A FINAL"
    # both contain a win phrase and mean the exact opposite. They belong to
    # the rules in section 4 below, so this must not swallow them.
    if (re.search(r"(won|win).{0,6}(a |an )?finals?( game| match)?\b"
                  r"|finals? (win|victory)", t)
            and not re.search(r"\bno\b|\bnever\b|\bwithout\b|\bzero\b", t)):
        return C.won_a_final(), "won a final"
    if re.search(r"played (in )?a final|finals? appearance", t):
        return C.played_in_a_final(), "played in a final"

    # 3d-bis. Any statistic, at any scope.
    #
    # One rule for the whole grid of (statistic × scope × total-or-average),
    # because splitting it produced gaps and contradictions: season totals
    # existed but career totals did not, so "500+ CAREER MARKS" was
    # unanswerable while "500+ MARKS IN A SEASON" worked; and "AVG 20+
    # DISPOSALS CAREER" fell through to the *season* average builder and
    # confidently answered a different question from the one asked.
    #
    # Scope is decided first, from the words present, then total versus
    # average. Game beats season beats career when several are named, since
    # "in a season" in "40+ DISPOSALS IN A GAME IN A SEASON" qualifies the
    # game. Finals is checked before all of them but after the dedicated
    # finals-average rule above, which owns the score-specific wording.
    # A cap ("LESS THAN 20 GOALS — CAREER", "50 OR FEWER GAMES") means the
    # opposite of everything below, and the builders here are all floors.
    # Falling through to the cap-aware rules further down is the only safe
    # thing to do: answering a cap with a floor is not a gap, it is a
    # confidently wrong answer to the question that was asked.
    # A stat total with no scope word at all ("20+ KICKS") means "in a single
    # game" to Gridley, but that reading is only safe once every later rule
    # has declined. Returning it here claimed criteria those rules own:
    # "30+ GOALS TWO DIFF CLUBS" became "30+ goals in a game" and
    # "30+ DISPOSALS & 3+ GOALS GAME" lost its second statistic. So the
    # reading is held here and answered at the very end of the function.
    implicit_game_stat = None

    stat_word = (None if _is_max(t)
                 else next((w for w in STAT_WORDS_BY_LENGTH if w in t), None))
    if stat_word:
        col = STAT_WORDS[stat_word]
        number = re.search(r"([\d.]+)\s*\+?", t)
        is_avg = bool(re.search(r"\bavg\b|\baverage[ds]?\b|\bper game\b", t))
        if number:
            raw = float(number.group(1))
            n = int(raw) if raw.is_integer() else raw

            # "10+ GAMES WITH 30+ DISPOSALS": two numbers, and the first
            # counts games rather than the statistic.
            repeat = re.search(
                r"(\d+)\+?\s*(?:games?|matches)\s*(?:with|of)\s*(\d+)", t)
            if repeat:
                times, threshold = int(repeat.group(1)), int(repeat.group(2))
                return (C.games_with_stat_min(col, threshold, times),
                        f"{times}+ games with {threshold}+ {col}")

            if "final" in t:
                if is_avg:
                    return (C.finals_stat_average_min(col, n),
                            f"{n}+ {col} avg in finals")
                # Plural "finals" with no single-game wording is the
                # career total across every final -- "KICKED 30+ GOALS IN
                # FINALS" -- where "in a final" stays one game. Reading
                # the plural as a single game answered a question about a
                # career with a bar nobody has cleared in one afternoon.
                if (re.search(r"\bfinals\b", t)
                        and not re.search(r"\bin (?:a|an|one) final\b"
                                          r"|\bsingle final\b", t)):
                    return (C.finals_stat_total_min(col, n),
                            f"{n}+ {col} in finals (career)")
                return (C.stat_in_a_final(col, n),
                        f"{n}+ {col} in a final")

            if re.search(r"\bin a (?:game|match)\b|\bsingle (?:game|match)\b"
                         r"|\bper game\b", t):
                if is_avg:
                    # "20+ PER GAME" with no season or career word is a
                    # career rate, which is the broader reading.
                    return (C.career_stat_average_min(col, n),
                            f"{n}+ {col} per game "
                            f"(min {C.CAREER_AVG_MIN_GAMES} games)")
                return C.stat_in_a_game(col, n), f"{n}+ {col} in a game"

            if re.search(r"\bseasons?\b", t) and "career" not in t:
                if is_avg:
                    return (C.season_stat_average_min(col, n),
                            f"{n}+ {col} avg in a season "
                            f"(min {C.SEASON_AVG_MIN_GAMES} games)")
                return (C.season_stat_total_min(col, n),
                        f"{n}+ {col} in a season")

            if re.search(r"\bcareers?\b|\btotal\b|\ball[- ]time\b", t):
                if is_avg:
                    return (C.career_stat_average_min(col, n),
                            f"{n}+ {col} avg per game in a career "
                            f"(min {C.CAREER_AVG_MIN_GAMES} games)")
                return (C.career_stat_total_min(col, n),
                        f"{n}+ {col} in a career")

            # An averaging word with no scope named at all. A season is the
            # narrower and far more common reading for a grid square, and
            # is what this rule has always answered.
            if is_avg:
                return (C.season_stat_average_min(col, n),
                        f"{n}+ {col} avg in a season "
                        f"(min {C.SEASON_AVG_MIN_GAMES} games)")
            
            # No scope named. Held as the last-resort reading rather than
            # returned; see the note where implicit_game_stat is declared.
            #
            # STAT_WORDS is matched by substring everywhere else in this
            # function, which is right when a scope word confirms the
            # reading and wrong when nothing does: "TOP 10 GOALKICKER"
            # contains "kick" and would otherwise be answered as "10+ kicks
            # in a game". A guess this weak has to see the whole word.
            if re.search(rf"\b{re.escape(stat_word)}e?s?\b", t):
                implicit_game_stat = (C.stat_in_a_game(col, n),
                                      f"{n}+ {col} in a game")

    # 3e. Season and club awards derivable from the data.
    m = re.search(r"(\d+)\s*x\s*leading goal ?kicker", t)
    if m and int(m.group(1)) > 1:
        n = int(m.group(1))
        return (C.leading_goalkicker_min(n),
                f"{n}x club leading goalkicker")
    if re.search(r"leading goal ?kicker", t):
        return C.leading_goalkicker(), "club leading goalkicker"
    # "MOST DISPOSALS TEAM": led the club's season tally of a statistic.
    # Goals stay with the official leading-goalkicker table; every other
    # statistic is ranked from the game rows.
    m = re.search(r"\bmost\s+(.+?)\s+(?:team|club)\b", t)
    if m:
        phrase = m.group(1)
        if re.search(r"\bgoals?\b", phrase):
            return C.leading_goalkicker(), "club leading goalkicker"
        for w in STAT_WORDS_BY_LENGTH:
            if w in phrase:
                col = STAT_WORDS[w]
                return (C.club_stat_leader(col, 1),
                        f"led club in {col} in a season")
    if re.search(r"wooden spoon", t):
        return C.wooden_spoon_player(), "wooden spoon season"
    if re.search(r"multi[- ]club", t):
        return C.multi_club_player(), "multi-club player"

    # 3e-bis. Match context: the size of the win, the size of the crowd.
    # Placed before the two-stat and single-game stat rules because "100+
    # POINT WIN" and "50,000+ CROWD" both carry a number and a noun that
    # those rules would otherwise try to read as a statistic.
    m = re.search(r"(\d[\d,]*)\+?\s*(?:people|fans|crowd|attendance|spectators)"
                  r"|crowd of (\d[\d,]*)", t)
    if m and re.search(r"crowd|attendance|people|fans|spectators", t):
        people = int((m.group(1) or m.group(2)).replace(",", ""))
        if _is_max(t):
            bound = _max_bound(t, people)
            return (C.crowd_max(bound), f"crowd of {bound:,} or fewer")
        if "final" in t:
            return (C.crowd_min_in_final(people),
                    f"crowd of {people:,}+ at a final")
        return C.crowd_min(people), f"crowd of {people:,}+"

    # Derby squares. C.derby_winning_record comes from match_constraints and
    # takes the derby it is about, so the fixture has to be named in the
    # text; calling it bare raised TypeError out of parse() instead of
    # declining the criterion.
    derby_key = next(
        (key for alias, key in DERBY_ALIASES.items()
         if re.search(rf"\b{re.escape(alias)}\b", t)), None)
    if derby_key:
        derby_label = C.DERBY_LABELS[derby_key]
        if re.search(r"\blosing record\b", t):
            return (C.derby_losing_record(derby_key),
                    f"{derby_label} losing record")
        if re.search(r"\bwinning record\b", t):
            return (C.derby_winning_record(derby_key),
                    f"{derby_label} winning record")
        # "SHOWDOWN KICKED A GOAL", "SYDNEY DERBY 5+ TACKLES": a feat
        # within the fixture. The scoring verb was stripped with the
        # league noise, so the stat word and an optional number remain.
        stat_word = next((w for w in STAT_WORDS_BY_LENGTH if w in t), None)
        if stat_word:
            col = STAT_WORDS[stat_word]
            n = _num(t, 1)
            return (C.derby_stat_in_game(derby_key, col, n),
                    f"{n}+ {col} in a {derby_label}")
        # "SHOWDOWN WINNER" asks for a win, not the whole winning record.
        if re.search(r"\bwinner\b|\bwon\b|\bwin\b", t):
            return C.derby_won(derby_key), f"won a {derby_label}"
        # "SHOWDOWN PLAYED IN 10+" and the older "10+ SHOWDOWN GAMES".
        m = re.search(r"(\d+)\+?\s*(?:games?|matches|times)\b"
                      r"|played(?:\s+in)?\s+(\d+)\s*\+?"
                      r"|(\d+)\s*\+\s*$", t)
        if m:
            count = int(m.group(1) or m.group(2) or m.group(3))
            return (C.derby_games_min(derby_key, count),
                    f"{count}+ {derby_label} games")
        return C.played_in_derby(derby_key), f"played in a {derby_label}"

    if re.search(r"\bderby\b", t):
        return None, "a derby criterion has to name which derby"

    # Marquee fixtures named by their day: Anzac Day, the Big Freeze (the
    # King's Birthday match since 2015), Dreamtime. These read the scraped
    # match_event tags, same as the marquee builders in the grid maker.
    marquee_hit = next(
        ((alias, event) for alias, event in (
            ("anzac day", "Anzac Day"),
            ("dreamtime", "Dreamtime at the 'G"),
            ("king's birthday", "King's Birthday"),
            ("kings birthday", "King's Birthday"),
        ) if alias in t), None)
    if marquee_hit:
        _alias, event = marquee_hit
        article = "an" if event[0].upper() in "AEIOU" else "a"
        if re.search(r"\bwinner\b|\bwon\b|\bwinning\b", t):
            return C.marquee_event_won(event), f"won {article} {event} match"
        m = re.search(r"(\d+)\s*\+?\s*(?:games?|matches|times)\b", t)
        if m:
            return (C.marquee_event_games_min(event, int(m.group(1))),
                    f"{m.group(1)}+ {event} matches")
        return (C.played_marquee_event(event),
                f"played {article} {event} match")
    if "big freeze" in t:
        return (C.marquee_event_played_since("King's Birthday", 2015),
                "played a Big Freeze match (King's Birthday, 2015 on)")

    if re.search(r"\bwinning record\b", t):
        return C.winning_record(), "winning record"

    m = re.search(r"(\d+)\+?\s*point (?:win|victory)"
                  r"|(?:win|won|winning).{0,10}by (\d+)", t)
    if m and not re.search(r"\bloss|\blost|\bdefeat", t):
        points = int(m.group(1) or m.group(2))
        if _is_max(t):
            bound = _max_bound(t, points)
            return (C.won_by_max(bound), f"won by {bound} points or fewer")
        return C.won_by_min(points), f"won by {points}+ points"

    m = re.search(r"(\d+)\+?\s*point (loss|defeat)|(?:lost|lose).{0,10}by (\d+)",
                  t)
    if m:
        points = int(m.group(1) or m.group(3))
        return C.lost_by_min(points), f"lost by {points}+ points"

    if re.search(r"\bdrawn? (match|game)\b|\btied game\b|played in a draw", t):
        return C.played_in_a_draw(), "played in a drawn match"

    # 3e-ter. "MORE FREES FOR THAN AGAINST": one career total over another.
    m = re.search(r"\bmore\s+(.+?)\s+than\s+(.+)$", t)
    if m:
        a = next((w for w in STAT_WORDS_BY_LENGTH if w in m.group(1)), None)
        b = next((w for w in STAT_WORDS_BY_LENGTH if w in m.group(2)), None)
        # The right-hand noun is often elided -- "…THAN AGAINST" -- so the
        # free-kick pair fills in its other half.
        if a and not b:
            if "against" in m.group(2) and STAT_WORDS[a] == "frees_for":
                b = "frees against"
            elif "for" in m.group(2) and STAT_WORDS[a] == "frees_against":
                b = "frees for"
        if a and b and STAT_WORDS[a] != STAT_WORDS[b]:
            return (C.career_stat_more_than(STAT_WORDS[a], STAT_WORDS[b]),
                    f"more career {STAT_WORDS[a]} than {STAT_WORDS[b]}")

    # 3f. Two stats in the same game: "30+ DISPOSALS & 3+ GOALS GAME"
    if "&" in t or " and " in t:
        pairs = re.findall(r"(\d+)\+?\s*([a-z ]+?)(?=\s*(?:&| and |$))", t)
        found = []
        for n, word in pairs:
            for w in STAT_WORDS_BY_LENGTH:
                col = STAT_WORDS[w]
                if w in word:
                    found.append((col, int(n)))
                    break
        if len(found) >= 2:
            (sa, na), (sb, nb) = found[0], found[1]
            return (C.two_stats_same_game(sa, na, sb, nb),
                    f"{na}+ {sa} & {nb}+ {sb}")

    # 4. Finals and premierships.
    if re.search(r"no finals win", t):
        return C.no_finals_wins(), "no finals wins"
    if re.search(r"never (won|win) a? ?final", t):
        return C.never_won_a_final(), "never won a final"
    if re.search(r"no finals|never played finals", t):
        return C.never_played_finals(), "never played finals"
    # A count reaches here when it was not written as "grand finals" -- the
    # rule in 3d only counts those. Without this, "2+ PREMIERSHIPS" was
    # answered with every single premiership player.
    m = re.search(r"(\d+)\+?\s*(?:premierships?|flags?)", t)
    if m and int(m.group(1)) > 1:
        count = int(m.group(1))
        return C.premierships_won_min(count), f"won {count}+ premierships"
    if re.search(r"premiership|flag|grand final win", t):
        return C.premiership_player(), "premiership player"

    # 5. One-club / multi-club.
    if re.search(r"one[- ]club", t):
        return C.one_club_player(), "one-club player"
    m = re.search(r"(\d+)\+?\s*(?:different\s+)?clubs?", t)
    if m and not re.search(r"goal|game", t):
        n = int(m.group(1))
        return C.played_for_n_clubs(n), f"{n}+ clubs"

    # 5b. Physicals
    m = re.search(r"(\d+)\+?\s*cm", t)
    if m:
        cm = int(m.group(1))
        if _is_max(t) or "under" in t or "shorter" in t:
            bound = _max_bound(t, cm)
            return C.height_max(bound), f"{bound} cm or shorter"
        return C.height_min(cm), f"{cm}+ cm tall"

    m = re.search(r"(\d+)\+?\s*kg", t)
    if m:
        kg = int(m.group(1))
        if _is_max(t) or "under" in t or "lighter" in t:
            bound = _max_bound(t, kg)
            return C.weight_max(bound), f"{bound} kg or lighter"
        return C.weight_min(kg), f"{kg}+ kg heavy"

    # 5c. Tenure and season-count squares that name no career keyword.
    # "200+ GAMES SAME CLUB" is loyalty, not a career total; "20+ GAMES IN
    # 2023" and "15 LOSSES SINGLE SEASON" are one year's workload; "10
    # WINS IN A ROW" is a streak of the player's own appearances.
    m = re.search(r"(\d+)\s*\+?\s*games?\b.{0,16}\b(?:same|single|one)\s+club",
                  t)
    if m:
        return (C.games_at_one_club_min(int(m.group(1))),
                f"{m.group(1)}+ games at one club")
    m = re.search(r"(\d+)\s*\+?\s*games? in (?:season )?((?:19|20)\d{2})\b", t)
    if m:
        return (C.games_in_season_min(int(m.group(1)), int(m.group(2))),
                f"{m.group(1)}+ games in {m.group(2)}")
    m = re.search(r"(\d+)\s*\+?\s*games?\b.{0,14}\b(?:a|one|single) season", t)
    if m:
        return (C.games_in_season_min(int(m.group(1))),
                f"{m.group(1)}+ games in a season")
    m = re.search(r"(\d+)\s*\+?\s*(wins|winning games?|losses|losing games?)"
                  r"\b.{0,16}\bseason", t)
    if m:
        n = int(m.group(1))
        if m.group(2).startswith(("loss", "losing")):
            return C.losses_in_season_min(n), f"{n}+ losses in a season"
        return C.wins_in_season_min(n), f"{n}+ wins in a season"
    m = re.search(r"(\d+)\s*\+?\s*(?:consecutive wins|wins in a row)", t)
    if m:
        return (C.wins_in_a_row(int(m.group(1))),
                f"{m.group(1)} wins in a row")

    # 6. "N+ goals/games for two different clubs".
    two_clubs = re.search(r"(two|2|three|3)\s*(?:diff\w*|different)?\s*clubs", t)
    if two_clubs:
        k = {"two": 2, "2": 2, "three": 3, "3": 3}[two_clubs.group(1)]
        n = _num(t)
        if "goal" in t and n:
            return (C.goals_at_multiple_clubs(n, k),
                    f"{n}+ goals at {k} clubs")
        if "game" in t and n:
            return (C.games_at_multiple_clubs(n, k),
                    f"{n}+ games at {k} clubs")

    # 7. Career games.
    # The second clause catches "UNDER 50 GAMES", which names no career
    # keyword at all. Rules 6 and 8 have already claimed the phrasings
    # where a bare "games" means something else ("50+ GAMES TWO DIFF
    # CLUBS", "40+ DISPOSALS IN A GAME"), so a cap word plus "games" here
    # can only be a career total.
    if (re.search(r"games? (played|career)|career games?|^\d+\+? games?$", t)
            or (_is_max(t) and re.search(r"\bgames?\b", t))):
        n = _num(t)
        if n:
            if _is_max(t):
                bound = _max_bound(t, n)
                return (C.career_games_max(bound),
                        f"{bound} or fewer games")
            return C.career_games_min(n), f"{n}+ games played"

    # 8. A stat threshold in a single game.
    if re.search(r"in a (game|match)|single game|\bgame\b|\bmatch\b", t):
        n = _num(t)
        for word in STAT_WORDS_BY_LENGTH:
            col = STAT_WORDS[word]
            if word in t and n:
                return C.stat_in_a_game(col, n), f"{n}+ {col} in a game"

    # 9. Career goals, floor or cap.
    # "LESS THAN 20 GOALS — CAREER" is career_goals_max(19). Without the
    # cap branch this fell through to career_goals_min(20) and answered
    # the exact opposite question.
    if "goal" in t:
        n = _num(t)
        if n and re.search(r"career|total|\d+\+? goals?$|goals? career", t):
            if _is_max(t):
                bound = _max_bound(t, n)
                return (C.career_goals_max(bound),
                        f"{bound} or fewer career goals")
            return C.career_goals_min(n), f"{n}+ career goals"

    # 10. Era / season range.
    m = re.search(r"(18|19|20)(\d0)s", t)
    if m:
        lo = int(m.group(1) + m.group(2))
        return C.played_in_season_range(lo, lo + 9), f"played in the {lo}s"
    m = re.search(r"(\d{4})\s*[-–]\s*(\d{4})", t)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return C.played_in_season_range(lo, hi), f"played {lo}-{hi}"

    # 11. A club name embedded in a longer phrase.
    for alias, club in CLUB_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", t):
            return C.played_for(club), club

    # 12. Last resort: a stat total whose scope was never named.
    if implicit_game_stat is not None:
        return implicit_game_stat

    # 13. Last resort: bare text that looks like a person's name.
    #
    # Gridley sometimes omits the word "teammate" entirely ("COLBY
    # MCKERCHER"). This has to be the final rule, not an early one: run
    # before the venue block it turned ADELAIDE OVAL, KARDINIA PARK,
    # VICTORIA PARK and seven other real grounds into teammate searches for
    # players who do not exist, which answer zero and cost a full sweep each.
    #
    # The vocabulary guard still earns its place at the end. A teammate
    # search is the most expensive thing this module can ask for, so any
    # criterion word that belongs to a rule above means the text is a
    # criterion this parser failed to read, not somebody's name.
    if (re.match(r"^[a-z]+(?: [a-z\-]+){1,3}$", t)
            and not re.search(_NOT_A_NAME, t)
            and not any(w in t for w in STAT_WORDS)):
        name = t.strip().title()
        # title() lowercases the C in "McKercher"; put it back.
        name = re.sub(r"\bMc([a-z])", lambda mc: f"Mc{mc.group(1).upper()}", name)
        return C.teammate_of(name), f"{name} teammate"

    return None, f"couldn't interpret: {text!r}"


def _fuzzy_correct(text):
    import difflib
    keywords = list(CLUB_ALIASES.keys()) + list(STAT_WORDS.keys()) + [
        "all-australian", "australian", "brownlow", "medal", "medallist", "coleman", "norm smith",
        "rising star", "nominee", "winner", "magarey", "sandover", "liston", "morrish",
        "larke", "hunter harrison", "best and fairest", "b&f", "draft", "pick",
        "father-son", "academy", "captain", "brother", "sibling", "sister", "twin",
        "father", "dad", "mother", "mum", "parent", "son", "daughter", "child",
        "grand", "cousin", "uncle", "aunt", "nephew", "niece", "in-law", "relative",
        "family", "teammate", "first", "debut", "career", "game", "match", "season",
        "goals", "finals", "grand final", "premiership", "flag", "win", "won",
        "loss", "lost", "defeat", "draw", "tied", "crowd", "attendance", "people",
        "fans", "spectators", "different", "clubs", "played", "avg", "average", "per game"
    ]
    
    words = text.split()
    corrected_words = []
    for w in words:
        if len(w) < 4 or re.match(r"^\d", w) or w == "teammate":
            corrected_words.append(w)
            continue
        matches = difflib.get_close_matches(w, keywords, n=1, cutoff=0.75)
        corrected_words.append(matches[0] if matches else w)
    return " ".join(corrected_words)


def parse(text, fuzzy=True):
    """
    Return (constraint, label) or (None, reason).
    `text` is one Gridley criterion, e.g. "30+ GOALS TWO DIFF CLUBS".
    If fuzzy=True, applies a spelling correction fallback for typos.
    """
    if not text:
        return None, "empty criterion"

    con, reason = _parse_exact(text)
    if con is not None:
        return con, reason

    if fuzzy:
        fuzzy_text = _fuzzy_correct(str(text).lower())
        if fuzzy_text != str(text).lower():
            fuzzy_con, fuzzy_reason = _parse_exact(fuzzy_text)
            if fuzzy_con is not None:
                return fuzzy_con, fuzzy_reason

    # The reason _parse_exact gave, not a generic one. UNSUPPORTED explains
    # *why* a criterion cannot be answered ("Rising Star nominations
    # unavailable"), and historic_grids shows that text to the player;
    # replacing every decline with "couldn't interpret" threw it away.
    return None, reason


def parse_grid(rows, cols):
    """Parse six criteria. Returns (parsed_rows, parsed_cols, problems)."""
    problems = []
    out = []
    for axis, items in (("row", rows), ("column", cols)):
        parsed = []
        for i, raw in enumerate(items, 1):
            con, label = parse(raw)
            if con is None:
                problems.append(f"{axis} {i} ({raw!r}): {label}")
                parsed.append((str(raw), None))
            else:
                parsed.append((label, con))
        out.append(parsed)
    return out[0], out[1], problems
