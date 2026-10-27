import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import os, sys, shutil, tempfile, types, importlib
sys.path.insert(0, _os.path.join(_REPO, "src"))
pkg = types.ModuleType("llamafone"); pkg.__path__ = [_os.path.join(_REPO, "src", "llamafone")]
sys.modules["llamafone"] = pkg
import llamafone.config as config

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails += 1

root = tempfile.mkdtemp()
logs = []
config._log = lambda m: logs.append(m)

def setup(profile, reg_docs):
    os.environ["USERPROFILE"] = profile
    os.environ["HOME"] = profile
    config._mods_folder_cache = None
    config._config = None
    config._last_config_path = None
    config.__file__ = os.path.join(root, 'elsewhere', 'Llamafone.ts4script', 'llamafone', 'config.pyc')
    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER = 0
    class K:
        def __enter__(s): return s
        def __exit__(s, *a): return False
    fake.OpenKey = lambda h, p: K()
    def q(k, n):
        if reg_docs is None: raise OSError
        return (reg_docs, 2)
    fake.QueryValueEx = q
    sys.modules["winreg"] = fake

# 1. Normal install: Documents in profile, cfg missing -> created there
p1 = os.path.join(root, "p1"); mods1 = os.path.join(p1, "Documents", "Electronic Arts", "The Sims 4", "Mods")
os.makedirs(mods1)
setup(p1, r"%USERPROFILE%\Documents")
check("normal: provider default", config.get_provider() == "claude")
check("normal: cfg created", os.path.isfile(os.path.join(mods1, "llamafone.cfg")))
check("normal: config_path", config.config_path() == os.path.join(mods1, "llamafone.cfg"))

# 2. OneDrive: profile/Documents has nothing; registry points to OneDrive
p2 = os.path.join(root, "p2"); mods2 = os.path.join(p2, "OneDrive", "Documents", "Electronic Arts", "The Sims 4", "Mods")
os.makedirs(mods2); os.makedirs(os.path.join(p2, "Documents"))
setup(p2, r"%USERPROFILE%\OneDrive\Documents")
config.get_config()
check("onedrive: cfg created in real Mods", os.path.isfile(os.path.join(mods2, "llamafone.cfg")))
check("onedrive: game folder", config.game_user_folder() == os.path.dirname(mods2))

# 3. Localized folder + user's ollama cfg with BOM
p3 = os.path.join(root, "p3"); mods3 = os.path.join(p3, "D_Docs", "Electronic Arts", "Die Sims 4", "Mods")
os.makedirs(mods3)
tmpl = config._DEFAULT_CONFIG_TEMPLATE.replace("provider = claude", "provider = ollama")
with open(os.path.join(mods3, "llamafone.cfg"), "w", encoding="utf-8-sig") as f:
    f.write(tmpl)
setup(p3, os.path.join(p3, "D_Docs"))
check("localized+BOM: provider ollama", config.get_provider() == "ollama")
check("localized+BOM: configured", config.is_configured())
# set_setting round trip keeps a single section
config.set_setting("auto_events_enabled", False)
txt = open(os.path.join(mods3, "llamafone.cfg"), encoding="utf-8-sig").read()
check("BOM set_setting: one section", txt.count("[llamafone]") == 1)
check("BOM set_setting: provider kept", config.get_provider() == "ollama")

# 4. No registry, no Mods anywhere -> None, logs where it looked
p4 = os.path.join(root, "p4"); os.makedirs(p4)
setup(p4, None)
check("none: provider default", config.get_provider() == "claude")
check("none: path None", config.config_path() is None and config.mods_folder() is None)
check("none: logged", any("no Mods folder found" in l for l in logs))

# 5. save_id legacy data stays put
import llamafone.save_id as save_id
p5 = os.path.join(root, "p5")
legacy = os.path.join(p5, "Documents", "Electronic Arts", "The Sims 4", "saves")
os.makedirs(os.path.join(legacy, "Llamafone"))
mods5 = os.path.join(p5, "OneDrive", "Documents", "Electronic Arts", "The Sims 4", "Mods"); os.makedirs(mods5)
setup(p5, r"%USERPROFILE%\OneDrive\Documents")
check("saves: legacy data kept", save_id._saves_folder() == legacy)
shutil.rmtree(os.path.join(legacy, "Llamafone"))
check("saves: real folder otherwise", save_id._saves_folder() == os.path.join(os.path.dirname(mods5), "saves"))

# 6. llamafone.cfg.txt next to the script (Notepad hidden-extension case)
p6 = os.path.join(root, "p6"); mods6 = os.path.join(p6, "Documents", "Electronic Arts", "The Sims 4", "Mods"); os.makedirs(mods6)
with open(os.path.join(mods6, "llamafone.cfg.txt"), "w", encoding="utf-8") as f:
    f.write(config._DEFAULT_CONFIG_TEMPLATE.replace("provider = claude", "provider = ollama"))
setup(p6, r"%USERPROFILE%\Documents")
check("txt: provider ollama", config.get_provider() == "ollama")
check("txt: no extra cfg created", not os.path.exists(os.path.join(mods6, "llamafone.cfg")))

# 7. Old ANSI (cp1252) cfg with an accented character still loads
p7 = os.path.join(root, "p7"); mods7 = os.path.join(p7, "Documents", "Electronic Arts", "The Sims 4", "Mods"); os.makedirs(mods7)
with open(os.path.join(mods7, "llamafone.cfg"), "w", encoding="cp1252") as f:
    f.write("[llamafone]\nprovider = ollama\nlanguage = Espa\u00f1ol\n")
setup(p7, r"%USERPROFILE%\Documents")
check("ansi: provider ollama", config.get_provider() == "ollama")
check("ansi: language read", config.get_language().startswith("Espa"))

# 8. Cached path reused; deleted cfg is re-found / re-created
calls = []
orig = config._mods_folder_candidates
config._mods_folder_candidates = lambda: (calls.append(1), orig())[1]
for _ in range(50): config.get_setting("x")
check("cache: no repeated folder scans", len(calls) == 0)
os.remove(os.path.join(mods7, "llamafone.cfg"))
config.get_setting("x")
check("cache: deleted cfg recreated", os.path.isfile(os.path.join(mods7, "llamafone.cfg")))
config._mods_folder_candidates = orig

shutil.rmtree(root)
print("FAILS", fails)



