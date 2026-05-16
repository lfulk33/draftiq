FANTASY_POSITIONS = {"QB", "RB", "WR", "TE"}

def filter_fantasy_players(players):
    filtered = {}
    for player_id, player in players.items():
        positions = player.get("fantasy_positions") or []
        if any(pos in FANTASY_POSITIONS for pos in positions):
            filtered[player_id] = player
    return filtered

def summarize(players):
    from collections import Counter
    counts = Counter()
    for p in players.values():
        for pos in (p.get("fantasy_positions") or []):
            if pos in FANTASY_POSITIONS:
                counts[pos] += 1
    for pos, count in sorted(counts.items()):
        print(f"{pos}: {count}")
    print(f"Total: {len(players)}")

if __name__ == "__main__":
    import json
    with open("players.json") as f:
        players = json.load(f)
    filtered = filter_fantasy_players(players)
    summarize(filtered)