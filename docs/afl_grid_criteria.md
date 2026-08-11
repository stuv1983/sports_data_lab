# AFL grid questions — the organised catalogue

Every axis the Grid Solver can build, grouped by the kind of
question a square asks. Generated from `afl/constraints.py`'s
`BUILDER_GROUPS` and `BUILDERS` — the same registry the Type
picker reads — so this list and the app cannot drift apart
quietly. Regenerate after adding a builder:

    python utils/afl/make_grid_criteria_doc.py

Optional-layer builders (draft, awards, Brownlow, Rising Star,
captaincy, family, match context) appear in the app only when
their data is loaded.

## Clubs & journeys

| Question | Arguments | What it answers |
|---|---|---|
| Played for club | club | Played at least one game for this club, predecessors included. |
| First career game for club | club | First career game was for this club. |
| One-club player | — |  |
| Multi-club player | — |  |
| Played for X+ clubs | clubs |  |
| X+ goals at 2+ clubs | goals, clubs | e.g. 30+ goals for each of two different clubs. |
| X+ games at 2+ clubs | games, clubs |  |

## Career milestones

| Question | Arguments | What it answers |
|---|---|---|
| 150+ / X+ career games | games |  |
| Fewer than X career games | games |  |
| X+ career goals | goals |  |
| X or fewer career goals | goals | At most n career goals/points -- INCLUSIVE of n. |
| X+ of a stat in a career | stat, x | Accumulated `n` or more of `stat` across a whole career. |
| Career average of a stat | stat, avg, min_games | Averaged `avg` or more of `stat` per game across a career. |
| Winning record in a derby | derby | Won more matches in this derby than they lost. |
| Club leading goalkicker | — | Led their club's goalkicking in at least one season. |

## Single-game feats

| Question | Arguments | What it answers |
|---|---|---|
| X+ of a stat in one game | stat, x |  |
| Two stats in the same game | stat_a, x_a, stat_b, x_b |  |
| X+ games with Y+ of a stat | stat, y, times | Reached `n` of `stat` in at least `times` separate games. |

## Season & era

| Question | Arguments | What it answers |
|---|---|---|
| Played in a decade | decade | Played at least one game in the decade starting `decade`. |
| Played between seasons | from, to |  |
| Debuted between seasons | from, to |  |
| X+ of a stat in one season | stat, x | Accumulated `n` or more of `stat` within a single season. |
| Season average of a stat | stat, avg | Averaged `avg` or more of `stat` per game across a whole season. |
| Wooden spoon season | — | Played for a club in a season it finished last. |
| Minor premiership | — | Played for a club in a season it finished top of the ladder. |
| No minor premierships | — | Never played a game for a club in a season it topped the ladder. |
| X+ minor premierships | times | Played in at least `times` seasons that ended in a minor premiership. |

## Finals & premierships

| Question | Arguments | What it answers |
|---|---|---|
| Played in a final | — |  |
| X+ finals games | x |  |
| Won a final | — | Won at least one post-season game (any round, any venue). |
| Never won a final | — | Includes players who never played a post-season game at all. |
| No finals wins (played finals) | — | Played at least one post-season game, never won one. |
| Never played finals | — |  |
| X+ of a stat in a final | stat, x | Reached `n` of `stat` in a single finals game. |
| X+ of a stat in a grand final | stat, x | Reached `n` of `stat` in a single grand final. |
| X+ of a stat in finals (career) | stat, x | Accumulated `n` or more of `stat` across a whole finals career. |
| Finals average of a stat | stat, avg | Averaged `avg` or more of `stat` across finals. |
| Goal average in finals | avg |  |
| Played a grand final | — |  |
| No grand finals | — | Never played in a grand final. |
| Played in X+ Grand Finals | times |  |
| Premiership player | — |  |
| Won X+ premierships | times |  |
| Lost X+ Grand Finals | times |  |

## Grounds & venues

| Question | Arguments | What it answers |
|---|---|---|
| Played at venue | venue |  |
| X+ games at venue | venue, x | Played `n` or more games at one venue. |
| Won a final at venue | venue |  |
| Ground performance | venue, ground_status, ground_metric, x | Players reaching a cumulative performance threshold at one ground. |

## Teammates

| Question | Arguments | What it answers |
|---|---|---|
| Teammate of… | player_id | Players who appeared in the same match for the same club. |
| Played with… | player_id | Players who represented the same club in the same season. |

## Physical

| Question | Arguments | What it answers |
|---|---|---|
| X+ cm tall | cm | Player is at least `cm` tall. |
| X cm or shorter | cm | Player is at most `cm` tall. |
| X+ kg | kg | Player is at least `kg` heavy. |
| X kg or lighter | kg | Player is at most `kg` heavy. |

## Draft & recruitment

| Question | Arguments | What it answers |
|---|---|---|
| Draft pick between | from, to | National Draft selection between two pick numbers, inclusive. |
| Draft type (National/Rookie…) | kind | National / Rookie / Pre-Season / Mid-Season / Trade / Free Agency. |
| Drafted between years | from, to |  |
| Drafted by club | club |  |
| Drafted by club, never played there | club | Drafted by a club, never played a senior game for it. |
| Recruited from… | source | Recruited through one step of the path to the draft. |

## Captaincy

| Question | Arguments | What it answers |
|---|---|---|
| Captain of club | club | Captained a particular historical or current club identity. |
| Captain of club between seasons | club, from, to | Captained a club during an inclusive season range. |
| Club captain | — | Was an AFL/VFL club captain in at least one recorded season. |
| Club captain between seasons | from, to | Was a club captain in any season in the inclusive range. |

## Awards & honours

| Question | Arguments | What it answers |
|---|---|---|
| Academy selection | — |  |
| All-Australian | times | Named in at least N All-Australian teams. |
| All-Australian between years | from, to |  |
| All-Australian captain | — |  |
| All-Australian squad | — |  |
| B&F at 2+ clubs | clubs |  |
| Brownlow Medallist | — |  |
| Club best and fairest | times |  |
| Club best and fairest at… | club | Won a club's best-and-fairest, respecting historical identities. |
| Coleman Medallist | — |  |
| Father-son selection | — | Recruited under the father-son rule -- so their father played. |
| Norm Smith Medallist | — |  |
| Number one draft pick | — |  |
| State-league medallist | — |  |
| Won an award X+ times | award, times |  |
| Won an award… | award | Appears on a given Draftguru award page. |
| Exact Brownlow finish | place |  |
| Top X Brownlow finish | place |  |
| Top X Brownlow finish X+ times | place, times |  |
| X+ Brownlow votes in a season | votes |  |
| Rising Star nominee | — |  |
| Rising Star nominee between seasons | from, to |  |
| Rising Star nominee for club | club |  |
| Rising Star nominee for club between seasons | club, from, to |  |
| Rising Star nominee in season | season |  |
| Rising Star nominee ineligible to win (suspension) | — | Nominated, but barred from winning because they were suspended. |

## Family

| Question | Arguments | What it answers |
|---|---|---|
| AFL/VFL family member | — | Listed in a family with at least one other trusted AFL/VFL player. |
| Brother also played AFL/VFL | — | Has an explicit brother/brothers relationship (twins included). |
| Extended family also played AFL/VFL | — | Grandparent, aunt/uncle, niece/nephew, cousin or in-law link. |
| Father/son also played AFL/VFL | — | Trusted parent/child link explicitly identifying a father or son. |
| Parent/child also played AFL/VFL | — |  |
| Relative played for club | club | Has another linked family member who played for ``club``. |
| Same listed family as… | player_id | Other AFL/VFL players in the same Wikipedia family section. |
| Sibling also played AFL/VFL | — | Has an explicitly identified sibling in the AFL/VFL player table. |

## Match context

| Question | Arguments | What it answers |
|---|---|---|
| Losing record in a derby | derby | Lost more matches in this derby than they won. |
| Played before a crowd of X or fewer | people | Played in front of a crowd of `people` or fewer. |
| Played before a crowd of X+ | people | Played in front of a crowd of `people` or more. |
| Played before a crowd of X+ in a final | people | A big crowd, at a final. |
| Played in a derby | derby | Played in this derby at all. |
| Played in a drawn match | — |  |
| Played in a loss by X+ points | points | Played in a loss by `points` or more. |
| Played in a marquee match | event | Played in this marquee fixture -- an Anzac Day match, a Dreamtime. |
| Played in a win by X or fewer | points | Played in a win by `points` or fewer -- a close win. |
| Played in a win by X+ points | points | Played in a win by `points` or more. |
| Team scored X+ points | points | Played in a match where their own team scored `points` or more. |
| Winning record in a derby | derby | Won more matches in this derby than they lost. |
| Winning record in a marquee match | event | Won more of this marquee fixture than they lost. |
| X+ games in a derby | derby, games | Played X+ matches in this derby, win or lose. |
| X+ marquee matches | event, games | Played X+ of this marquee fixture. |
