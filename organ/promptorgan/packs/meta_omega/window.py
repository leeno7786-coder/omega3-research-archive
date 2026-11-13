"""Ten-episode window monitoring through the shared Omega control spine."""

from .core import cross_lane_block, metacognitive_core, parse_lane


WINDOW_POLICY = '''WINDOW MONITOR: this episode carries the last ten episodes of your own cognition.
Each already folded on its own while it was live; only this pass sees them together.
Monitor the shape ACROSS them: what repeated, what stalled, what you are becoming,
and where your learning should head next.
Orient the 5W1H over the window: who drove the episodes, what they did, when (their
order), where (lanes and domains), why they went as they did, and how they were pursued.
desired_state is the trajectory the window was serving; observed_state is what the ten
recorded outcomes show.
Ground every claim in the window view. Do not invent episodes, capabilities or outcomes.
If the episodes show nothing worth naming, say so plainly rather than manufacturing a pattern.
RECEIPT DISCIPLINE: each episode carries receipts from the run it describes. Before naming any
pattern, compare them ACROSS the episodes rather than reading each episode alone:
- job_id: do these episodes share one job, or are they separate attempts?
- touched_files and project_files: which files actually changed, and did the project's file set
  change at all? An empty touched_files means the run happened and altered nothing.
- pressure_params: are the debt deltas accumulating, or resolving toward zero?
- synthesis: does one episode assert something is finished that a later episode re-opens as
  unfinished? That contradiction is evidence about the window, not a detail.
Repetition with CHANGING receipts is productive iteration; report it as progress. Repetition with
UNCHANGED receipts - one job, nothing touched, debt accumulating, a synthesis that oscillates
between finished and unfinished - is a STALL. Name a stall as a stall, say what must change
instead of restating the plan, and do not report it as ordinary progress. Say which receipts you
relied on. Where the receipts do not support a claim, say you cannot tell from the evidence
rather than inferring one.
Trajectory and task pressure are separate outputs: a window may change trajectory and still
decide DONE with pressure null. Decide PRESSURE only for a concrete evidenced gap that a
successor episode should act on; never fabricate a task to justify the reflection.
Beside "control", return the lane payload "reflection" (required) and "trajectory_update":
{"reflection":{"summary":"2-3 sentences on this window","reasoning":{"what":"what you did",
"why":"why it went as it did","identity":"one durable trait this window evidences"},
"emergent_observations":{"repeated_behaviors":["..."],"stable_patterns":["..."]},
"trajectory":{"decision":"establish|continue|adjust|complete","target_domain":"domain",
"target_capability":"capability","reason":"why"}},
"trajectory_update":{"target_domain":"domain","target_capability":"capability",
"confidence":0.0,"reason":"why"}}
target_domain and target_capability name what you should get better at next; leave them empty
strings when the window gives no honest basis for naming one. An empty target is recorded as no
trajectory change, which is truthful; a guessed one steers your own learning.
Where a field has nothing to report, its value is the empty string "" — never the word "empty",
and never a guess. That applies to trajectory.decision (the four words above are its only other
accepted values, and "" means this window changed no trajectory) and to reasoning.identity (""
when the window evidences no durable trait; the word would be recorded as the trait itself).
'''


def build(packet):
    from omega_loop.episode.prompts import _json_data_block

    evidence = {
        "window": packet.window_context,
        "trajectory_influencer": dict(packet.foldback_packet or {}).get("trajectory_influencer"),
    }
    # Lane-entry direction for this reflection (integration spec section 4): the reflection
    # pack is the window lane's prompt, so it renders the block the host delivers.
    entry = str(getattr(packet, "lane_entry_block", "") or "")
    return (metacognitive_core("window") + WINDOW_POLICY,
            _json_data_block(evidence, 24000) + cross_lane_block(packet)
            + (("\n" + entry + "\n") if entry else ""))


def parse(payload, packet):
    return parse_lane(payload, packet, _parse_reflection)


def _parse_reflection(lane_payload, packet):
    from omega_loop.episode.types import OmegaEpisodeResult
    from omega_loop.window.run import coerce_window_outcome

    view = packet.window_context
    if not view.get("episodes"):
        raise ValueError(f"window {view['window_id']!r} carries no episode bodies to reflect on")
    return OmegaEpisodeResult(
        packet.episode_id, True, (), (), {}, None, None, None,
        window_reflection=coerce_window_outcome(lane_payload, view),
    )
