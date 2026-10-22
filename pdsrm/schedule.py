"""Round-robin schedule: all dyads, grouped into rounds of disjoint pairs, with balanced initiators."""
from collections import Counter

from .util import substream


def circle_rounds(agents):
    """Classic circle method. Odd n gets a bye, so each round has floor(n/2) disjoint dyads."""
    players = list(agents)
    if len(players) % 2:
        players.append(None)
    m = len(players)
    rounds = []
    for _ in range(m - 1):
        pairs = []
        for i in range(m // 2):
            a, b = players[i], players[m - 1 - i]
            if a is not None and b is not None:
                pairs.append((a, b))
        rounds.append(pairs)
        players = [players[0], players[-1]] + players[1:-1]
    return rounds


def initiator_map(agents, rng):
    """Odd n: every agent initiates exactly (n-1)/2 times (a regular tournament).
    Even n: half the agents initiate n/2 times, half n/2 - 1 times."""
    order = list(agents)
    rng.shuffle(order)
    n = len(order)
    init = {}

    def circulant(group):
        m = len(group)
        for i in range(m):
            for d in range(1, (m - 1) // 2 + 1):
                j = (i + d) % m
                init[frozenset((group[i], group[j]))] = group[i]

    if n % 2 == 1:
        circulant(order)
    else:
        core, last = order[:-1], order[-1]
        circulant(core)
        others = core[:]
        rng.shuffle(others)
        k = n // 2 - 1
        for idx, o in enumerate(others):
            init[frozenset((last, o))] = last if idx < k else o
    return init


def build_schedule(agents, seed, group, loop):
    rng_s = substream(seed, "schedule", group, loop)
    rng_i = substream(seed, "initiator", group, loop)
    shuffled = list(agents)
    rng_s.shuffle(shuffled)
    rounds = circle_rounds(shuffled)
    rng_s.shuffle(rounds)
    init = initiator_map(agents, rng_i)

    schedule = []
    for r_idx, pairs in enumerate(rounds, start=1):
        pairs = list(pairs)
        rng_s.shuffle(pairs)
        rnd = []
        for d_idx, (a, b) in enumerate(pairs, start=1):
            ini = init[frozenset((a, b))]
            resp = b if ini == a else a
            rnd.append({
                "dyad_id": f"g{group:02d}_l{loop:02d}_r{r_idx:02d}_d{d_idx}",
                "group": group, "loop": loop, "round": r_idx,
                "initiator": ini, "responder": resp,
            })
        schedule.append(rnd)

    # sanity checks
    n = len(agents)
    all_dyads = [frozenset((d["initiator"], d["responder"])) for rnd in schedule for d in rnd]
    assert len(all_dyads) == n * (n - 1) // 2 == len(set(all_dyads)), "schedule is not a full round robin"
    for rnd in schedule:
        seen = [x for d in rnd for x in (d["initiator"], d["responder"])]
        assert len(seen) == len(set(seen)), "an agent appears twice in one round"
    if n % 2 == 1:
        counts = Counter(d["initiator"] for rnd in schedule for d in rnd)
        assert all(counts[a] == (n - 1) // 2 for a in agents), "initiators not balanced"
    return schedule
