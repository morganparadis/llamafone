"""
Affordance injection -- adds our custom phone-UI interaction to the
sim object tuning at game load time.

Sims 4 mods can't synthesise a brand-new SuperInteraction at runtime
from pure Python -- the game's tuning pipeline requires an XML tuning
backed by a .package. What we control from Python is which OBJECTS the
tuned interaction shows up on. Our .package ships the Claude_PhoneMenu
tuning (with a phone-routing category set inside it -- internally a
PieMenuCategory in the engine type system, but it targets the phone
wheel because of the Appropriateness_Phone tag and ACTOR target_type)
and this module appends it to the Sim object's _super_affordances
tuple after instance load.
"""
import os
import datetime


# Tuning name our companion .package creates -- the S4S tunable
# instance for the interaction uses this as its instance name. We
# look it up by name from the INTERACTION manager rather than by GUID
# so the same Python keeps working if the package is ever regenerated.
_PHONE_MENU_TUNING_NAME = "Claude_PhoneMenu"


def _log(msg):
    """Append to the main mod log so we can tell what got injected."""
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "ClaudeAI_Log.txt")
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
                # tuning name is sometimes the snake_case form -- match either way
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


def _append_affordance(obj_tuning, new_aff):
    """Mutate the object tuning's super-affordance set to include `new_aff`.
    _super_affordances is a tuple in the game, so we rebuild it."""
    try:
        current = list(getattr(obj_tuning, "_super_affordances", ()) or ())
        if new_aff in current:
            return False  # already there, no-op
        current.append(new_aff)
        obj_tuning._super_affordances = tuple(current)
        return True
    except Exception as e:
        _log(f"append_affordance({obj_tuning.__name__}) failed: {type(e).__name__}: {e}")
        return False


_PHONE_CATEGORY_TUNING_NAME = "Claude_PhoneCategory"
# Type ID for PieMenuCategory resources (Sims 4 tuning system).
# https://www.thesims4.dev/types -- 0x03E9D964 = PieMenuCategory.
_TYPE_PIE_MENU_CATEGORY = 0x03E9D964


def _dump_interaction_details(name, tuning):
    """Spit a few load-time properties of an interaction tuning into the log."""
    try:
        cat = getattr(tuning, "category", None)
        cat_id = getattr(cat, "guid64", None) if cat is not None else None
        cat_name = getattr(cat, "__name__", None) if cat is not None else None
        apps = getattr(tuning, "appropriateness_tags", None)
        cats = getattr(tuning, "interaction_category_tags", None)
        target = getattr(tuning, "target_type", None)
        cls = getattr(tuning, "__name__", None)
        mro_tail = [b.__name__ for b in getattr(tuning, "__mro__", ())[:5]]
        _log(
            f"  tuning {name}: class={cls} mro_head={mro_tail} "
            f"category={cat_name}({cat_id}) target_type={target} "
            f"appropriateness={apps} interaction_cats={cats}"
        )
    except Exception as e:
        _log(f"  tuning {name}: dump failed: {type(e).__name__}: {e}")


def _diagnose_phone_categories():
    """List every PieMenuCategory tuning currently loaded -- helps confirm
    our Claude_PhoneCategory was successfully read out of the .package."""
    try:
        import services
        from sims4.resources import Types
        # PieMenuCategory has its own resource type, mapped via Types if available
        pie_type = getattr(Types, "PIE_MENU_CATEGORY", None)
        if pie_type is None:
            # Fall back to enum-value lookup
            for name in dir(Types):
                if "PIE" in name.upper() and "CATEGORY" in name.upper():
                    pie_type = getattr(Types, name)
                    break
        if pie_type is None:
            _log("diagnose_categories: could not locate PIE_MENU_CATEGORY Types enum value.")
            return
        mgr = services.get_instance_manager(pie_type)
        if mgr is None:
            _log("diagnose_categories: pie_menu_category instance manager not ready.")
            return
        count = 0
        found_ours = None
        for key, cls in mgr.types.items():
            name = getattr(cls, "__name__", "?")
            count += 1
            if "Claude" in name:
                found_ours = (key, name, cls)
        _log(f"diagnose_categories: {count} PieMenuCategory tunings total.")
        if found_ours:
            key, name, cls = found_ours
            icon = getattr(cls, "_icon", None)
            disp = getattr(cls, "_display_name", None)
            _log(f"  -> FOUND Claude category: key={hex(key) if isinstance(key, int) else key} "
                 f"name={name} icon={icon} display_name={disp}")
        else:
            _log("  -> Claude_PhoneCategory NOT loaded. Package may be misformatted, "
                 "or PieMenuCategory resource type ID is wrong in the packer.")
    except Exception as e:
        _log(f"diagnose_categories failed: {type(e).__name__}: {e}")


def _inject_affordances():
    """Run after all OBJECT tunings are loaded. Look up our phone
    interaction and graft it onto every Sim object tuning."""
    # Step 1: diagnose what's actually loaded from our .package
    _diagnose_phone_categories()
    phone_menu = _find_interaction_tuning(_PHONE_MENU_TUNING_NAME)
    if phone_menu is None:
        _log("Inject skipped -- Claude_PhoneMenu tuning not found. Is the .package installed?")
        return
    _dump_interaction_details(_PHONE_MENU_TUNING_NAME, phone_menu)

    try:
        import services
        from sims4.resources import Types
        obj_mgr = services.get_instance_manager(Types.OBJECT)
        if obj_mgr is None:
            _log("Inject skipped -- OBJECT manager not ready.")
            return

        sim_hits = 0
        first_sim = None
        for key, tuning in obj_mgr.types.items():
            try:
                if _looks_like_sim(tuning):
                    if _append_affordance(tuning, phone_menu):
                        sim_hits += 1
                        if first_sim is None:
                            first_sim = tuning
            except Exception:
                continue

        _log(f"Inject complete -- phone menu on {sim_hits} sim tuning(s).")
        # Sanity-check: confirm our affordance is actually in the sim's list now.
        if first_sim is not None:
            try:
                affs = list(getattr(first_sim, "_super_affordances", ()) or ())
                ours_in = phone_menu in affs
                _log(f"  post-inject sample sim has {len(affs)} _super_affordances; "
                     f"Claude_PhoneMenu present: {ours_in}")
            except Exception:
                pass
    except Exception as e:
        _log(f"_inject_affordances failed: {type(e).__name__}: {e}")


def register():
    """Register the inject callback with the OBJECT instance manager.
    Called once during mod init (from claude_ai.__init__)."""
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
