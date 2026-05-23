import copy
from ..utils.parsing import parse_facet_output


def clear_timeline_from_timestep(timeline, start_timestep):
    for t in range(start_timestep - 1, len(timeline)):
        timeline[t]["facets"] = []


def clear_all_but_implied_from_timestep(timeline, start_timestep):
    for t in range(start_timestep, len(timeline) + 1):
        step = timeline[t - 1]
        step["facets"] = [
            block for block in step.get("facets", []) if block.get("type") == "implied"
        ]


def add_facet_to_timestep(timeline, timestep, facet_type, facets, caused_by=None):
    entry = {"type": facet_type, "facets": facets}
    if caused_by:
        entry["causedBy"] = caused_by
    timeline[timestep - 1]["facets"].append(entry)


def fetch_and_add_optional_facets(service, timeline, timestep):
    try:
        optionals = service.send_command(f"? {timestep}")
        if optionals:
            add_facet_to_timestep(timeline, timestep, "optional", optionals)
        return []
    except Exception as e:
        return [{"type": "optional-fetch", "timestep": timestep, "error": str(e)}]


def fetch_and_add_empty_facets(service, timeline, timestep, used_facet_ids=None):
    try:
        all_open = service.send_command("?")
        open_facets = [f for f in all_open if f.get("timestep") == timestep]
        if used_facet_ids is not None:
            open_facets = [f for f in open_facets if f.get("id") not in used_facet_ids]
        if open_facets:
            add_facet_to_timestep(timeline, timestep, "empty", open_facets)
        return []
    except Exception as e:
        return [{"type": "open-fetch", "timestep": timestep, "error": str(e)}]


def fetch_and_replace_empty_facets(service, timeline, timestep):
    step = timeline[timestep - 1]

    if any(
        f.get("type") in ("plan", "implied", "selected") for f in step.get("facets", [])
    ):
        return []

    try:
        empty_facets = service.send_command(f"? {timestep}")

        used_facet_ids = set()
        for f in step.get("facets", []):
            for facet in f.get("facets", []):
                if "id" in facet:
                    used_facet_ids.add(facet["id"])

        filtered_empty = [f for f in empty_facets if f.get("id") not in used_facet_ids]

        # Remove previous empty facets
        step["facets"] = [f for f in step.get("facets", []) if f.get("type") != "empty"]

        if filtered_empty:
            step["facets"].append({"type": "empty", "facets": filtered_empty})

        return []
    except Exception as e:
        return [{"type": "empty-fetch", "timestep": timestep, "error": str(e)}]


def fetch_and_add_implied_facets(
    service, timeline, global_implied_ids, base_facet_id, horizon
):
    errors = []
    try:
        implied = service.send_command("|= %")
        for f in implied:
            implied_id = f["id"]
            ts = f.get("timestep")

            if ts is None or not (1 <= ts <= horizon):
                # Invalid timestep, skip
                continue

            # Check if facet already present at the timestep
            found = False
            for item in timeline[ts - 1]["facets"]:
                if item["type"] == "implied":
                    existing_ids = {facet["id"] for facet in item["facets"]}
                    if implied_id in existing_ids:
                        # Append to impliedBy if not already present
                        if base_facet_id not in item.get("impliedBy", []):
                            item.setdefault("impliedBy", []).append(base_facet_id)
                        found = True
                        break

            if not found:
                f["selectionState"] = "+"
                # Add a new implied facet entry
                timeline[ts - 1]["facets"].append(
                    {"type": "implied", "facets": [f], "impliedBy": [base_facet_id]}
                )

            # Track it globally regardless of duplication or activation
            global_implied_ids.add(implied_id)

    except Exception as e:
        errors.append({"type": "implied-fetch", "error": str(e)})

    return errors


def undo_facets(service, step_data, timeline):
    errors = []

    for step in step_data:
        for block in step.get("facets", []):
            if block.get("type") in ("selected", "plan"):
                for facet in block.get("facets", []):
                    facet_id = facet.get("id")
                    selection_state = facet.get("selectionState", "Not selected")
                    if facet_id and selection_state != "Not selected":
                        if selection_state == "+":
                            undo_cmd = f"- {facet_id}"
                        elif selection_state == "-":
                            undo_cmd = f"- ~{facet_id}"
                        else:
                            continue
                        try:
                            # Send command to undo the facet
                            service.send_command(undo_cmd, no_Output=True)

                            # Remove facet_id from all impliedBy lists in timeline
                            for timestep in timeline:
                                # We collect implied blocks to remove (with empty impliedBy) in a separate list
                                blocks_to_remove = []
                                for implied_block in timestep.get("facets", []):
                                    if implied_block.get("type") == "implied":
                                        if facet_id in implied_block.get(
                                            "impliedBy", []
                                        ):
                                            implied_block["impliedBy"].remove(facet_id)

                                            # If impliedBy is now empty, mark this block for removal
                                            if not implied_block["impliedBy"]:
                                                blocks_to_remove.append(implied_block)

                                # Now actually remove the marked blocks
                                for block_to_remove in blocks_to_remove:
                                    timestep["facets"].remove(block_to_remove)

                        except Exception as e:
                            errors.append(
                                {
                                    "undo-error": f"- {facet_id}",
                                    "error": str(e),
                                    "timestep": step["timestep"],
                                }
                            )

    return errors


def getAllImpliedAndActivatedIds(timeline):
    global_implied_ids = set()
    activated_plan_ids = set()

    for step in timeline:
        for facet_group in step.get("facets", []):
            facet_type = facet_group.get("type")
            for facet in facet_group.get("facets", []):
                facet_id = facet.get("id")
                if facet_id is None:
                    continue
                if facet_type == "implied":
                    global_implied_ids.add(facet_id)
                elif facet_type == "plan":
                    activated_plan_ids.add(facet_id)
                elif facet_type == "selected":
                    activated_plan_ids.add(facet_id)
    return global_implied_ids, activated_plan_ids


def prepare_timeline_for_update(service, changed_timestep):
    # Save current facets for every step from the change point onward
    saved_steps = []
    for t in range(changed_timestep, service.horizon + 1):
        step = service.timeline[t - 1]
        saved_steps.append({"timestep": t, "facets": step.get("facets", [])})

    # Clear timeline steps
    clear_all_but_implied_from_timestep(service.timeline, changed_timestep)
    global_implied_ids, activated_plan_ids = getAllImpliedAndActivatedIds(
        service.timeline
    )
    # Undo all existing facet activations
    errors = undo_facets(service, reversed(saved_steps), service.timeline)
    return saved_steps, global_implied_ids, activated_plan_ids, errors


def apply_user_command(service, t, command, global_implied_ids):
    # TODO: If is negative add and on the timestep then is a positive one implied -> add it there -> how to show exactly??
    errors = []
    stripped = command.strip()
    is_remove = stripped.startswith("-")
    is_negative_add = stripped.startswith("+ ~")
    is_positive_add = stripped.startswith("+") and not is_negative_add
    clean_cmd = stripped.lstrip("+-~ ").strip()

    if is_remove:
        # Check if there are other facets remaining at this timestep
        current_facets = service.timeline[t]["facets"]
        non_removed_facets_exist = any(
            f.get("facets") for f in current_facets if f.get("facets")
        )

        # TODO: Now does later not fetch anymore the empty ones, need to fix this

        # Only fetch optionals if the timestep still has other facets
        if non_removed_facets_exist:
            try:
                errors.extend(
                    fetch_and_add_optional_facets(service, service.timeline, t)
                )
            except Exception as e:
                errors.append({"command-error": command, "error": str(e)})
    else:
        errors.extend(fetch_and_add_optional_facets(service, service.timeline, t))
        try:
            service.send_command(command, no_Output=True)
            parsed_facets = parse_facet_output(clean_cmd, command)
            for facet in parsed_facets:
                if is_negative_add:
                    facet["selectionState"] = "-"
                elif is_positive_add:
                    facet["selectionState"] = "+"
            add_facet_to_timestep(service.timeline, t, "selected", parsed_facets)

            errors.extend(
                fetch_and_add_implied_facets(
                    service,
                    service.timeline,
                    global_implied_ids,
                    clean_cmd,
                    service.horizon,
                )
            )
        except Exception as e:
            errors.append({"command-error": command, "error": str(e)})
    return errors


def reactivate_facets_from_step(self, t, step_data, optional_ids, global_implied_ids):
    errors = []
    reactivated_any = False

    for block in step_data.get("facets", []):
        if block.get("type") in (
            "selected",
            "plan",
        ):  # Consider including "implied" if needed
            for facet in block.get("facets", []):
                facet_id = facet.get("id")
                selection_state = facet.get("selectionState", "Not selected")
                if (
                    facet_id
                    and facet_id in optional_ids
                    and selection_state != "Not selected"
                ):
                    if selection_state == "+":
                        cmd = f"+ {facet_id}"
                    elif selection_state == "-":
                        cmd = f"+ ~{facet_id}"
                    else:
                        continue
                    try:
                        self.send_command(cmd, no_Output=True)
                        reactivated_any = True

                        add_facet_to_timestep(
                            self.timeline, t, block.get("type"), [facet]
                        )

                        # TODO: Maybe need to update all implied ones? Does not do it correctly right now
                        errors.extend(
                            fetch_and_add_implied_facets(
                                self,
                                self.timeline,
                                global_implied_ids,
                                facet_id,
                                self.horizon,
                            )
                        )
                    except Exception as e:
                        errors.append(
                            {"reactivate-error": f"{facet_id}", "error": str(e)}
                        )

    return errors, reactivated_any


###########################################################################################################


def fast_apply_user_command(self, command: str, t: int, errors: list):
    stripped = command.strip()
    is_remove = stripped.startswith("-")
    is_negative_add = stripped.startswith("+ ~")
    is_positive_add = stripped.startswith("+") and not is_negative_add

    # Extract clean id
    clean_id = stripped.lstrip("+-~ ").strip()

    # Case 1: Remove
    if is_remove:
        handle_remove_command(self, command, t, clean_id, errors)
        return

    # Case 2: Add (+ or +~)
    new_state = "-" if is_negative_add else "+"

    if is_positive_add:
        handle_existing_positive_selection(self, t, clean_id, errors)

    # Detect conflict: same id but different sign in this timestep
    handle_conflicting_selection(self, t, clean_id, new_state, errors)

    # Apply the new command
    try:
        self.send_command(command, no_Output=True)
    except Exception as e:
        errors.append({"command-error": command, "error": str(e)})

    # TODO: delete it?
    # Add new facet info to timeline
    try:
        parsed_facets = parse_facet_output(clean_id, command)
        for f in parsed_facets:
            f["selectionState"] = new_state
        add_facet_to_timestep(self.timeline, t, "selected", parsed_facets)
    except Exception as e:
        errors.append({"parse-error": command, "error": str(e)})


def handle_remove_command(self, command: str, t: int, clean_id: str, errors: list):
    try:
        self.send_command(command, no_Output=True)
    except Exception as e:
        errors.append({"command-error": command, "error": str(e)})
        return

    # Remove from current timestep
    step = self.timeline[t - 1]
    for block in list(step.get("facets", [])):
        if block.get("type") not in ("selected", "plan"):
            continue

        block["facets"] = [
            f for f in block.get("facets", []) if f.get("id") != clean_id
        ]
        if not block["facets"]:
            step["facets"].remove(block)

    # Remove implieds caused by this facet
    cleanup_implied_by(self, clean_id)


def handle_conflicting_selection(
    self, t: int, clean_id: str, new_state: str, errors: list
):
    # If the same facet id already exists in the current timestep with a different selectionState,
    # undo it before applying the new command
    step = self.timeline[t - 1]

    for block in step.get("facets", []):
        if block.get("type") not in ("selected", "plan"):
            continue

        for facet in list(block.get("facets", [])):
            if facet.get("id") == clean_id:
                old_state = facet["selectionState"]

                # Only act if it’s actually different
                if old_state != new_state:
                    undo_cmd = f"- {clean_id}" if old_state == "+" else f"- ~{clean_id}"
                    try:
                        self.send_command(undo_cmd, no_Output=True)
                        block["facets"].remove(facet)
                        if not block["facets"]:
                            step["facets"].remove(block)
                    except Exception as e:
                        errors.append(
                            {"undo-error": undo_cmd, "error": str(e), "timestep": t}
                        )
                return


def handle_existing_positive_selection(self, t: int, new_id: str, errors: list):
    # If a '+' facet already exists in timestep t, undo it and remove it locally
    # This allows replacing the old '+' with a new '+' in one step
    step = self.timeline[t - 1]

    for block in list(step.get("facets", [])):
        if block.get("type") not in ("selected", "plan"):
            continue

        for facet in list(block.get("facets", [])):
            if facet.get("selectionState") == "+":
                old_id = facet.get("id")
                if old_id == new_id:
                    # It's the same + facet, nothing to undo
                    return

                undo_cmd = f"- {old_id}"
                try:
                    self.send_command(undo_cmd, no_Output=True)
                    block["facets"].remove(facet)
                    if not block["facets"]:
                        step["facets"].remove(block)
                    cleanup_implied_by(self, old_id)
                except Exception as e:
                    errors.append(
                        {"undo-error": undo_cmd, "error": str(e), "timestep": t}
                    )

                # Only one '+' allowed, so we stop after removing it
                return


def cleanup_implied_by(self, facet_id: str):
    for step in self.timeline:
        for block in list(step.get("facets", [])):
            if block.get("type") == "implied":
                implied_by = block.get("impliedBy", [])
                if facet_id in implied_by:
                    implied_by.remove(facet_id)
                    if not implied_by:
                        step["facets"].remove(block)


def fetch_implied_facets(self, errors: list):
    # Get implied facets using '|= %'
    try:
        return self.send_command("|= %")
    except Exception as e:
        errors.append({"type": "implied-fetch", "error": str(e)})
        return []


def fetch_removed_facets(self, errors: list):
    # Get facets that are no longer valid using '|= %%'
    try:
        return self.send_command("|= %% occurs")
    except Exception as e:
        errors.append({"type": "removed-fetch", "error": str(e)})
        return []


def add_implied_facets(self, implied_facets, base_facet_id: str, errors: list):
    # Add new implied facets to the timeline
    # Avoid duplicates and maintain impliedBy
    # TODO: Remove implieds if there are not anymore there
    # TODO: Implied should be all in the same
    for f in implied_facets:
        implied_id = f.get("id")
        ts = f.get("timestep")
        if not implied_id or not ts or ts < 1 or ts > self.horizon:
            continue

        step = self.timeline[ts - 1]
        found = False

        # Check if this implied facet is already present
        for block in step.get("facets", []):
            if block.get("type") == "implied":
                existing_ids = {facet.get("id") for facet in block.get("facets", [])}
                if implied_id in existing_ids:
                    # Update impliedBy if needed
                    if base_facet_id not in block.get("impliedBy", []):
                        block.setdefault("impliedBy", []).append(base_facet_id)
                    found = True
                    break

        if not found:
            # Add new implied facet
            f["selectionState"] = "+"
            step["facets"].append(
                {"type": "implied", "facets": [f], "impliedBy": [base_facet_id]}
            )


def remove_facets_from_timeline(self, removed_facets, changed_timestep, errors: list):
    # Remove facets that are no longer selectable by iterating timeline from changed_timestep onward
    # This is more efficient than looping over the huge removed_facets list

    # Build a set of removed ids for quick lookup
    removed_ids = {f.get("id") for f in removed_facets if f.get("id")}

    for t in range(changed_timestep, self.horizon + 1):
        step = self.timeline[t - 1]

        for block in list(step.get("facets", [])):
            # Only consider blocks that could be removed (optional, selected, plan)
            if block.get("type") != "optional":
                continue

            old_facets = block.get("facets", [])
            # Only remove facets that are not present in selected or plan
            selected_ids = {
                f.get("id")
                for b in step.get("facets", [])
                if b.get("type") in ("selected", "plan")
                for f in b.get("facets", [])
                if f.get("id")
            }

            new_facets = [
                f
                for f in old_facets
                if f.get("id") not in removed_ids or f.get("id") in selected_ids
            ]

            if len(new_facets) < len(old_facets):
                removed_now = [
                    f.get("id")
                    for f in old_facets
                    if f.get("id") not in selected_ids and f.get("id") in removed_ids
                ]
                for f_id in removed_now:
                    errors.append({"removed": f_id, "timestep": t})

            block["facets"] = new_facets

            if not block["facets"]:
                step["facets"].remove(block)


def calculate_facet_count(self):
    if not hasattr(self, "timeline") or self.timeline is None:
        return 0

    count = 0
    for timestep in self.timeline:
        facets = timestep.get("facets", [])

        # Separate types
        optional_facets = [f for f in facets if f.get("type") == "optional"]
        empty_facets = [f for f in facets if f.get("type") == "empty"]

        # Count 'optional' only if it's the only outer facet
        if len(facets) == 1 and optional_facets:
            count += len(optional_facets[0].get("facets", [])) * 2

        # Count all inner facets of 'empty' facets
        for f in empty_facets:
            count += len(f.get("facets", [])) * 2

    return count


# ========================================================================


def refresh_optionals_and_empties(self):
    try:
        # Take snapshot of the current timeline (not mutated)
        old_timeline = copy.deepcopy(self.timeline)

        temp_process = self.restart_FASB()

        errors, new_timeline = rebuild_timeline_from_solver(self, old_timeline)

        facet_count = self.send_command("#?")

        # Update the main timeline and related data directly
        self.timeline = new_timeline

        print("Timeline refresh complete and ready.")

        return errors, facet_count

    except Exception as e:
        print(f"Timeline refresh failed: {e}")
        if getattr(self, "process", None):
            try:
                self._terminate_process(self.process)
                self.process = None
            except Exception:
                pass


def rebuild_timeline_from_solver(self, old_timeline):
    # TODO: does always add twice the facets, does not work correctly for implied at beginning, shows plan ones as selected
    new_timeline = [{"timestep": t, "facets": []} for t in range(1, self.horizon + 1)]
    global_implied_ids = set()
    activated_plan_ids = set()
    errors = []

    # First collect global implied facets from plan root context
    errors.append(
        fetch_and_add_implied_facets(
            self, new_timeline, global_implied_ids, "plan", self.horizon
        )
    )

    for t in range(1, self.horizon + 1):

        step = new_timeline[t - 1]["facets"]

        errors.append(fetch_and_add_optional_facets(self, new_timeline, t))

        old_step = old_timeline[t - 1]["facets"]
        for block in old_step:
            if block.get("type") not in ("plan", "selected"):
                continue

            for facet in block.get("facets", []):
                fid = facet.get("id")
                if not fid or fid in activated_plan_ids:
                    continue

                # Determine command and type
                if block.get("type") == "plan":
                    facet_type = "plan"
                    cmd_str = f"+ {fid}"
                elif block.get("type") == "selected":
                    facet_type = "selected"
                    state = facet.get("selectionState")
                    cmd_str = f"+ {fid}" if state == "+" else f"+ ~{fid}"

                # Send command and add facet
                try:
                    self.send_command(cmd_str, no_Output=True)
                    add_facet_to_timestep(new_timeline, t, facet_type, [facet])
                    activated_plan_ids.add(fid)
                except Exception as e:
                    errors.append({"action": cmd_str, "timestep": t, "error": str(e)})

                # Recalculate implied facets for this facet
                errors.append(
                    fetch_and_add_implied_facets(
                        self, new_timeline, global_implied_ids, fid, self.horizon
                    )
                )

        # Fill empty facets if no plan or implied facets exist
        if not any(f["type"] in ("plan", "selected", "implied") for f in step):
            errors.extend(fetch_and_add_empty_facets(self, new_timeline, t))

        # If the step contains only implied facets, re-add them as optional
        elif all(f["type"] == "implied" for f in step):
            try:
                implied_facets = []
                for implied_block in step:
                    implied_facets.extend(implied_block.get("facets", []))
                if implied_facets:
                    add_facet_to_timestep(new_timeline, t, "optional", implied_facets)
                else:
                    errors.extend(fetch_and_add_empty_facets(self, new_timeline, t))
            except Exception as e:
                errors.append(
                    {
                        "type": "readd-implied-as-optional",
                        "timestep": t,
                        "error": str(e),
                    }
                )

    return errors, new_timeline
