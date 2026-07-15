import re
from typing import Dict, List


def parse_facet_output(output: str, command: str) -> List[Dict]:
    def make_facet(action_str, timestep, raw_id):
        parts = [p.strip().strip('"') for p in action_str.split(",")]
        return {
            "id": raw_id,
            "action": parts[0],
            "arguments": parts[1:],
            "constant1": parts[1] if len(parts) > 1 else None,
            "constant2": parts[2] if len(parts) > 2 else None,
            "timestep": int(timestep),
            "reduction": {
                "solution": {"positive": None, "negative": None},
                "facets": {"positive": None, "negative": None},
            },
            "remaining": {
                "solution": {"positive": None, "negative": None},
                "facets": {"positive": None, "negative": None},
            },
            "selectionState": "Not selected",
        }

    if command.startswith(("?", "|= %", "+", "-")):
        facets = []
        pattern = r"(occurs(?:_sometime)?\(action\(\(([^)]+)\)\)(?:,(\d+))?\))"
        matches = re.findall(pattern, output)
        for full_match, action_str, timestep in matches:
            ts = int(timestep) if timestep else 0
            facets.append(make_facet(action_str, ts, full_match))
        return facets

    elif command.startswith(("#??", "#!!")):
        facets = {}

        for line in output.strip().splitlines():
            line = line.strip()
            while line.startswith("::"):
                line = line[2:].strip()

            match = re.match(
                r"([0-9.]+)\s+([0-9.]+)\s+(~?)(occurs(?:_sometime)?\(action\(\(([^)]+)\)\)(?:,(\d+))?\))",
                line,
            )
            if not match:
                continue

            val1_str, val2_str, negated, full_match, action_str, timestep = match.groups()
            ts = int(timestep) if timestep else 0
            key = (action_str, ts)

            if key not in facets:
                facets[key] = make_facet(action_str, ts, full_match)

            facet = facets[key]
            target = "solution" if command == "#!!" else "facets"
            sign = "negative" if negated else "positive"

            facet["reduction"][target][sign] = float(val1_str)
            facet["remaining"][target][sign] = float(val2_str)

        return list(facets.values())

    return []


def parse_solution_output(output: str) -> List[Dict]:
    solutions = []
    solution_blocks = re.split(r"solution (\d+):", output.strip())

    for i in range(1, len(solution_blocks), 2):
        solution_number = solution_blocks[i]
        actions_block = solution_blocks[i + 1]
        current_actions = []

        pattern = r"(occurs(?:_sometime)?\(action\(\(([^)]+)\)\)(?:,(\d+))?\))"
        action_matches = re.findall(pattern, actions_block)

        for full_match, action_str, timestep in action_matches:
            parts = [p.strip().strip('"') for p in action_str.split(",")]
            action_type = parts[0]
            ts = int(timestep) if timestep else 0

            action_dict = {
                "id": full_match,
                "action": action_type,
                "arguments": parts[1:],
                "constant1": parts[1] if len(parts) > 1 else None,
                "constant2": parts[2] if len(parts) > 2 else None,
                "timestep": ts,
                "reduction": None,
                "remaining": None,
            }
            current_actions.append(action_dict)

        solutions.append(
            {"label": f"solution {solution_number}", "facets": current_actions}
        )

    return solutions
