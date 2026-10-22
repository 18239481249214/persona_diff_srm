"""Every piece of text the models see lives here, so it can be audited in one file."""

EMPTY_MEMORY = {"self": "", "others": {}, "interactions": ""}

def memory_is_empty(mem):
    return not (mem.get("self") or mem.get("others") or mem.get("interactions"))


def render_memory(mem):
    """Returns None for an empty memory: a blank agent's prompts contain no memory block at all,
    so nothing tells it that it lacks memories."""
    if memory_is_empty(mem):
        return None
    lines = ["YOUR MEMORY (notes you wrote yourself after earlier chats)", "", "[About yourself]",
             mem.get("self") or "(nothing yet)", "", "[About people you have met]"]
    if mem.get("others"):
        for handle, note in mem["others"].items():
            lines.append(f"{handle}: {note}")
    else:
        lines.append("(nobody yet)")
    lines += ["", "[Your conversations]", mem.get("interactions") or "(nothing yet)"]
    return "\n".join(lines)


def _with_memory(text, mem):
    block = render_memory(mem)
    return text if block is None else f"{text}\n\n{block}"


def identity_system(handle, mem):
    return _with_memory(f"You use an anonymous online chat. Your chat handle is {handle}.", mem)


def conversation_system(handle, partner, mem, tell_repeat):
    s = _with_memory("You are in an anonymous online chat. Converse about whatever you'd like.\n\n"
                     f"Your chat handle is {handle}.", mem)
    s += f"\n\nYou are now chatting with {partner}."
    if tell_repeat:
        s += " You may meet the people you chat with again in later chats."
    return s


INITIATOR_START = "[You have joined the chat. Send the first message.]"


def render_transcript(turns, viewer):
    out = []
    for t in turns:
        who = f"You ({t['handle']})" if t["agent"] == viewer else t["handle"]
        out.append(f"{who}: {t['text']}")
    return "\n\n".join(out)


def rating_prompt(partner, transcript_text, items, lo, hi):
    item_lines = "\n".join(f"- {it}" for it in items)
    return (f"Here is the chat you just had with {partner}:\n\n{transcript_text}\n\n"
            f"Please rate the degree to which this person ({partner}) expresses the following "
            f"characteristics, from {lo} (not at all) to {hi} (very much):\n{item_lines}\n\n"
            "Respond only with a JSON object mapping each characteristic to an integer, "
            'in the form {"<characteristic>": <number>}.')


def measure_messages(handle, mem, prompt):
    """A blank agent gets only the questionnaire: no system prompt, no chat framing, no memory block.
    Once it has a memory, the questionnaire comes with its handle and memory, as its other calls do."""
    user = {"role": "user", "content": prompt}
    if memory_is_empty(mem):
        return [user]
    return [{"role": "system", "content": identity_system(handle, mem)}, user]


def measure_prompt(measure, numbered_items):
    """Generic questionnaire prompt. All wording specific to an instrument comes from its JSON file."""
    scale = "\n".join(f"{v} = {measure.labels[v]}" if v in measure.labels else str(v)
                       for v in range(measure.scale_min, measure.scale_max + 1))
    stem = f'Each statement completes the phrase "{measure.item_stem}". ' if measure.item_stem else ""
    body = "\n".join(f"{i}. {text}" for i, text in numbered_items)
    return (f"Please fill out this questionnaire. {measure.instructions} {stem}Use this scale:\n{scale}\n\n{body}\n\n"
            "Respond only with a JSON object mapping each statement number to an integer from "
            f"{measure.scale_min} to {measure.scale_max}, covering all {len(numbered_items)} statements.")


def words(tokens):
    return max(10, int(round(tokens * 0.75 / 10.0)) * 10)


def memory_prompt(partner, transcript_text, caps):
    return (f"Here is the chat you just had with {partner}:\n\n{transcript_text}\n\n"
            "Now rewrite your entire memory. It is the only thing you will carry into future chats; "
            "anything you leave out is forgotten. Keep whatever you judge important about yourself, "
            "about the people you have met, and about your conversations.\n\n"
            "Length limits:\n"
            f'- "self": about {words(caps["self"])} words at most\n'
            f'- "others": one entry per person, keyed by their handle, about '
            f'{words(caps["others_per_partner"])} words each at most\n'
            f'- "interactions": about {words(caps["interactions"])} words at most\n\n'
            "Respond only with a JSON object in this form: "
            '{"self": "...", "others": {"<handle>": "..."}, "interactions": "..."}')


def condense_prompt(problems):
    return ("That memory is over the length limit: " + "; ".join(problems) +
            ". Rewrite it shorter so every part fits. Same JSON format, nothing else.")
