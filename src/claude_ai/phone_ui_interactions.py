"""
Custom phone-UI interaction for Claude AI.

Adds a "Claude AI" tile to the sim's phone wheel. In the game's
internal type system this involves a PieMenuCategory tuning -- the
engine reuses one radial-menu category type for object pie menus,
sim pie menus, the phone wheel, computer menus, tablet menus etc.
Ours is named Claude_PhoneCategory so the intent stays clear; it
targets the phone wheel specifically because:
  - Appropriateness_Phone is on the interaction
  - target_type is ACTOR (the holding sim)
  - the icon resource shows up as a tile on the phone home grid

Clicking the tile opens our menu interaction; the interaction
triggers a notification (POC) and will trigger the settings dialog
chain in the next release.

Phone-routable interactions must use SuperInteraction (not
ImmediateSuperInteraction) -- the phone-wheel filter silently drops
Immediate variants. We subclass SuperInteraction and fire our Python
in _run_interaction_gen while the brief phone animation (Phone_Text)
plays in parallel.
"""
from interactions.base.super_interaction import SuperInteraction

from . import notifications


class ClaudePhoneMenuInteraction(SuperInteraction):
    """Phone-wheel option -> opens the Claude AI menu.

    POC: notification only, so we can confirm the wiring works in-game.
    Once that's proven, _run_interaction_gen swaps to opening a dialog
    chain for Settings (global config) and Manage Contacts (per-sim prefs).
    """

    def _run_interaction_gen(self, timeline):
        try:
            notifications.show(
                "Claude AI",
                "Phone menu wiring confirmed -- the interaction fired "
                "correctly.\n\nSettings and per-sim contact preferences "
                "will live here in the next release.",
            )
        except Exception:
            pass
        # Let the base class do its normal animation/exit handling so
        # the sim cleanly finishes the phone gesture.
        result = yield from super()._run_interaction_gen(timeline)
        return result
