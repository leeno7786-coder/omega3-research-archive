"""Exactly four Omega prompt classes, selected by the explicit packet lane."""


def foldback_spec_for(packet):
    from omega_loop.episode.prompts import LANE_SPECS

    try:
        return LANE_SPECS[packet.omega_lane]
    except (KeyError, AttributeError) as exc:
        raise ValueError("Omega foldback requires a valid omega_lane") from exc


__all__ = ["foldback_spec_for"]
