def normalize_facets(facets):
    return [normalize_facet(facet) for facet in facets]


def normalize_implied_facets(facets):
    normalized = []
    seen_ids = set()
    for facet in facets or []:
        facet_id = facet.get("id")
        if not facet_id or facet_id in seen_ids:
            continue
        normalized.append(normalize_facet(facet, facet_type="implied"))
        seen_ids.add(facet_id)
    return normalized


def normalize_facet(facet, facet_type=None, parent_id=None):
    action_name = facet.get("action", "")
    arguments = facet.get("arguments")
    if arguments is None:
        arguments = [
            constant
            for constant in [facet.get("constant1"), facet.get("constant2")]
            if constant
        ]
    label = " ".join([action_name, *arguments]).strip()
    abstract_time_step = is_abstract_facet(facet)
    facet_timestep = None if abstract_time_step else facet.get("timestep")

    normalized = {
        "id": facet["id"],
        "label": label or facet["id"],
        "timestep": facet_timestep if facet_timestep else None,
        "selectionState": normalize_selection_state(facet.get("selectionState")),
        "action": {"name": action_name, "arguments": list(arguments)},
    }

    if facet_type:
        normalized["facetType"] = facet_type
    if facet_type == "implied":
        normalized["selectionState"] = "neutral"
        normalized["selectable"] = False
    if abstract_time_step:
        normalized["abstractTimeStep"] = True
    if parent_id:
        normalized["parentId"] = parent_id
    if facet.get("reduction") is not None:
        normalized["reduction"] = facet["reduction"]
    if facet.get("remaining") is not None:
        normalized["remaining"] = facet["remaining"]

    return normalized


def is_abstract_facet(facet):
    return facet.get("id", "").startswith("occurs_sometime(")


def normalize_count(value):
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise ValueError("FASB returned no numeric count.")
    for line in reversed(value.splitlines()):
        stripped = line.strip()
        while stripped.startswith("::"):
            stripped = stripped[2:].strip()
        if stripped.isdecimal():
            return int(stripped)
    if not value.strip():
        raise ValueError("FASB returned no numeric count.")
    raise ValueError(f"FASB returned an invalid count: {value!r}")


def normalize_selection_state(selection_state):
    if selection_state == "+":
        return "positive"
    if selection_state == "-":
        return "negative"
    return "neutral"


def build_selection_command(facet_id: str, selection_state: str):
    if selection_state == "positive":
        return f"+ {facet_id}"
    if selection_state == "negative":
        return f"+ ~{facet_id}"
    raise ValueError("Unsupported facet selection state.")


def build_solution_command(solution_number):
    if solution_number is None:
        return "!"
    if type(solution_number) is int and solution_number > 0:
        return f"! {solution_number}"
    raise ValueError("solutionNumber must be a positive integer.")


def normalize_solutions(solutions):
    return [normalize_solution(solution) for solution in solutions]


def normalize_solution(solution):
    raw_facets = [
        facet
        for facet in solution.get("facets", [])
        if not is_abstract_facet(facet)
    ]
    raw_facets.sort(
        key=lambda facet: (
            facet.get("timestep") is None,
            facet.get("timestep") or 0,
            facet.get("id", ""),
        )
    )

    facets = []
    previous_facet_id = None
    for raw_facet in raw_facets:
        normalized = normalize_facet(
            raw_facet,
            facet_type="plan",
            parent_id=previous_facet_id,
        )
        facets.append(normalized)
        previous_facet_id = normalized["id"]

    return {
        "label": solution.get("label", ""),
        "facets": facets,
    }
