import re
from typing import Dict, List


def parse_facet_output(output: str, command: str) -> List[Dict]:
    def make_facet(action_str, timestep, raw_id):
        parts = [p.strip().strip('"') for p in action_str.split(",")]
        return {
            "id": raw_id,
            "action": parts[0],
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
        tokens = output.strip().split()
        facets = {}
        key_to_id = {}

        for i in range(0, len(tokens) - 2, 3):
            val1_str = tokens[i]
            val2_str = tokens[i + 1]
            action_part = tokens[i + 2]
            match = re.search(
                r"(occurs(?:_sometime)?\(action\(\(([^)]+)\)\)(?:,(\d+))?\))",
                action_part,
            )
            if not match:
                continue

            full_match, action_str, timestep = match.groups()
            ts = int(timestep) if timestep else 0
            key = (action_str, ts)

            if key not in facets:
                key_to_id[key] = full_match
                facets[key] = make_facet(action_str, ts, full_match)

            facet = facets[key]
            target = "solution" if command == "#!!" else "facets"
            sign = "negative" if "~" in action_part else "positive"

            facet["reduction"][target][sign] = float(val1_str)
            facet["remaining"][target][sign] = float(val2_str)

        return list(facets.values())

    return []


def parse_solution_output(output: str) -> List[Dict]:
    solutions = []
    action_id = 0
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
            const1 = parts[1] if len(parts) > 1 else None
            const2 = parts[2] if len(parts) > 2 else None
            ts = int(timestep) if timestep else 0

            action_dict = {
                "id": full_match,
                "action": action_type,
                "constant1": const1,
                "constant2": const2,
                "timestep": ts,
                "reduction": None,
                "remaining": None,
            }
            current_actions.append(action_dict)
            action_id += 1

        solutions.append(
            {"label": f"solution {solution_number}", "facets": current_actions}
        )

    return solutions


def extract_plan_actions(plan_file_path: str) -> List[Dict]:
    actions = []
    timestep = 1
    with open(plan_file_path, "r") as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith(";"):
                if line.startswith("(") and line.endswith(")"):
                    parts = line[1:-1].lower().split()
                    action_str = ",".join(f'"{p}"' for p in parts)
                    formatted = f"occurs(action(({action_str})),{timestep})"
                    actions.append(formatted)
                    timestep += 1
    return parse_facet_output(" ".join(actions).strip(), "?")
