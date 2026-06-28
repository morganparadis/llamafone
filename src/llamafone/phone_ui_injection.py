"""
Affordance injection -- adds our phone-UI interactions to the Sim
object tuning at game load time.

Sims 4 mods can't synthesise a brand-new SuperInteraction at runtime
from pure Python -- the game's tuning pipeline requires an XML tuning
backed by a .package. What we control from Python is which OBJECTS the
tuned interaction shows up on. Our .package ships three interactions
(Llamafone_Call, Llamafone_Text, Llamafone_Settings) whose `category` points at
the in-game phoneCategory_Social tile (instance 0x19DBF). This module
appends them to the Sim object tuning's `_phone_affordances` after
instance load so they appear under Phone > Social.
"""
import os
import datetime


# Tuning names our .package ships. We look these up by name from the
# INTERACTION manager rather than by GUID so the same Python keeps
# working if the package is ever regenerated with different IDs.
_PHONE_INTERACTION_NAMES = (
    "Llamafone_Call",
    "Llamafone_Text",
    "Llamafone_Settings",
)


def _log(msg):
    """Append to the main mod log so we can tell what got injected."""
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [phone_ui] {msg}\n")
    except Exception:
        pass


def _find_interaction_tuning(name):
    """Look up an interaction tuning class by name from the INTERACTION manager."""
    try:
        import services
        from sims4.resources import Types
        mgr = services.get_instance_manager(Types.INTERACTION)
        if mgr is None:
            return None
        for key, cls in mgr.types.items():
            try:
                if getattr(cls, "__name__", "") == name:
                    return cls
                if getattr(cls, "tuning_name", "") == name:
                    return cls
            except Exception:
                continue
    except Exception as e:
        _log(f"_find_interaction_tuning({name}) failed: {type(e).__name__}: {e}")
    return None


def _looks_like_sim(obj_tuning):
    """Is this the Sim object tuning? Match by class module + name."""
    try:
        cls = obj_tuning
        mod = getattr(cls, "__module__", "") or ""
        name = getattr(cls, "__name__", "") or ""
        if mod.startswith("sims.sim") and name == "Sim":
            return True
        for base in getattr(cls, "__mro__", ()):
            try:
                bmod = getattr(base, "__module__", "") or ""
                bname = getattr(base, "__name__", "") or ""
                if bmod.startswith("sims.sim") and bname == "Sim":
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _append_affordance(obj_tuning, new_aff, attr_name="_phone_affordances"):
    """Mutate the named affordance tuple on the tuning to include `new_aff`.

    Phone-wheel items live under `_phone_affordances` (which the Sim's
    `potential_phone_interactions()` iterates). Regular pie menus use
    `_super_affordances` -- the phone filter ignores those."""
    try:
        current = list(getattr(obj_tuning, attr_name, ()) or ())
        if new_aff in current:
            return False
        current.append(new_aff)
        setattr(obj_tuning, attr_name, tuple(current))
        return True
    except Exception as e:
        _log(f"append_affordance({obj_tuning.__name__}, {attr_name}) failed: "
             f"{type(e).__name__}: {e}")
        return False


def _inject_affordances():
    """Run after all OBJECT tunings are loaded. Look up each Llamafone phone
    interaction by name and graft it onto every Sim object tuning's
    `_phone_affordances`."""
    tunings = []
    for tuning_name in _PHONE_INTERACTION_NAMES:
        cls = _find_interaction_tuning(tuning_name)
        if cls is None:
            _log(f"Inject: {tuning_name} not found -- skipping.")
            continue
        tunings.append((tuning_name, cls))

    if not tunings:
        _log("Inject aborted -- no Llamafone phone interactions loaded from .package.")
        return

    try:
        import services
        from sims4.resources import Types
        obj_mgr = services.get_instance_manager(Types.OBJECT)
        if obj_mgr is None:
            _log("Inject skipped -- OBJECT manager not ready.")
            return

        sim_tunings = [t for k, t in obj_mgr.types.items() if _looks_like_sim(t)]
        per_interaction = {}
        for tuning_name, cls in tunings:
            count = 0
            for sim_tuning in sim_tunings:
                if _append_affordance(sim_tuning, cls, "_phone_affordances"):
                    count += 1
            per_interaction[tuning_name] = count

        summary = ", ".join(f"{n}={c}" for n, c in per_interaction.items())
        _log(f"Inject complete -- sim tunings touched per interaction: {summary}")
    except Exception as e:
        _log(f"_inject_affordances failed: {type(e).__name__}: {e}")


def register():
    """Register the inject callback with the OBJECT instance manager.
    Called once during mod init (from llamafone.__init__)."""
    try:
        import services
        from sims4.resources import Types
        obj_mgr = services.get_instance_manager(Types.OBJECT)
        if obj_mgr is None:
            _log("register() deferred -- OBJECT manager not ready yet.")
            return
        obj_mgr.add_on_load_complete(lambda _mgr: _inject_affordances())
        _log("register() OK -- inject callback wired up.")
    except Exception as e:
        _log(f"register() failed: {type(e).__name__}: {e}")
