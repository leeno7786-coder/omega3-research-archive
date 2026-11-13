"""Permanent semantic lanes bound to physical execution pipelines."""

PIPELINE_IDS: tuple[int, ...] = (0, 1, 2, 3)
PIPELINE_COUNT = len(PIPELINE_IDS)
LANES: tuple[str, ...] = ("chat", "browser", "agent", "window")
PIPELINE_TO_LANE = dict(zip(PIPELINE_IDS, LANES, strict=True))
LANE_TO_PIPELINE = {lane: pipeline_id for pipeline_id, lane in PIPELINE_TO_LANE.items()}
AGENT_CONTRIBUTOR_TYPES = frozenset(
    {"agent_motor_dispatch", "agent_continuation", "agent_boot_restore"}
)
CAUSAL_PARENT_KEY = "causal_parent_episode_id"


def validate_pipeline_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in PIPELINE_IDS:
        raise ValueError(f"unknown pipeline_id {value!r}; expected one of {PIPELINE_IDS}")
    return value


def validate_lane(value: object) -> str:
    if type(value) is not str or value not in LANES:
        raise ValueError(f"unknown semantic lane {value!r}; expected one of {LANES}")
    return value


def lane_for_pipeline(pipeline_id: object) -> str:
    return PIPELINE_TO_LANE[validate_pipeline_id(pipeline_id)]


def pipeline_for_lane(lane: object) -> int:
    return LANE_TO_PIPELINE[validate_lane(lane)]


def lane_for_contributor(contributor: object) -> str:
    """Resolve the host-owned destination lane for one admitted contributor.

    User-authored operator turns always enter Chat so Global A can classify them
    inside a pressure-opened episode. Host-routed follow-up work carries an
    explicit semantic lane; agent and reflection contributor types retain their
    fixed ownership. Every other machine contributor must also carry an explicit
    validated lane; there is no implicit lane default.
    """
    metadata = getattr(contributor, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    explicit = getattr(contributor, "lane", None) or metadata.get("lane")
    if getattr(contributor, "source", None) == "operator":
        if explicit is not None and validate_lane(explicit) != "chat":
            raise ValueError("operator_turn_must_enter_chat_lane")
        return "chat"
    kind = getattr(contributor, "type", None)
    fixed = "agent" if kind in AGENT_CONTRIBUTOR_TYPES else (
        "window" if kind == "window_reflection_ready" else None
    )
    if fixed is not None:
        for identity in (getattr(contributor, "lane", None), metadata.get("lane"),
                         metadata.get("omega_lane")):
            if identity is not None and validate_lane(identity) != fixed:
                raise ValueError(f"{kind} has fixed semantic lane {fixed!r}")
        return fixed
    if explicit is not None:
        return validate_lane(explicit)
    omega_lane = metadata.get("omega_lane")
    if omega_lane is not None:
        return validate_lane(omega_lane)
    # No implicit Chat identity: the producer must bind a semantic lane.
    raise ValueError(
        f"machine contributor {getattr(contributor, 'type', None)!r} has no semantic lane"
    )


def work_kind_for_contributor(contributor: object) -> str:
    restored = getattr(contributor, "work_kind", None)
    if restored is not None:
        if type(restored) is not str or not restored.strip():
            raise ValueError("invalid contributor work_kind")
        return restored
    if getattr(contributor, "source", None) == "operator":
        return "operator_turn"
    kind = getattr(contributor, "type", None)
    if kind == "lane_work_dispatch":
        return "operator_lane_work"
    if kind in AGENT_CONTRIBUTOR_TYPES:
        return "agent_continuation"
    if kind == "window_reflection_ready":
        return "reflection"
    return "autonomous"
