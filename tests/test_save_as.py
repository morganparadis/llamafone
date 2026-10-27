import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import sys, types, os, shutil, tempfile, hashlib
REPO=_REPO
sys.path.insert(0, REPO+"/src")
for n in ("services","sims4","sims4.commands","sims4.log"): sys.modules.setdefault(n, types.ModuleType(n))
pkg=types.ModuleType("llamafone"); pkg.__path__=[REPO+"/src/llamafone"]; sys.modules["llamafone"]=pkg
NOTES=[]
notif=types.ModuleType("llamafone.notifications"); notif.show=lambda t,m,*a,**k: NOTES.append(m); sys.modules["llamafone.notifications"]=notif; pkg.notifications=notif
ms=types.ModuleType("llamafone.milestones"); ms.start_background_scan=lambda: None; sys.modules["llamafone.milestones"]=ms; pkg.milestones=ms
import llamafone.save_id as sid
LOG=[]; sid._log=lambda m: LOG.append(m)
T=tempfile.mkdtemp(); root=os.path.join(T,"Llamafone"); os.makedirs(root)
A="Slot_00001115"
# A sample save folder: the files a real one has, with made-up content.
os.makedirs(os.path.join(root, A, "sub"))
for name, body in (("Journal.json", '[{"sim": "Vivian", "content": "hi honey"}]'),
                   ("SimBios.json", '{"1": "Famous singer"}'),
                   ("SaveNotes.json", '{"v": 1, "notes": "Story rules"}'),
                   ("Feed.json", '{"posts": []}'),
                   (os.path.join("sub", "extra.json"), '{}')):
    open(os.path.join(root, A, name), "w", encoding="utf-8").write(body)
sid._saves_folder=lambda: T
sid._current_household_key=lambda: "hh"; sid._get_current_slot_name=lambda: "x"
cur={"id":A}; sid.get_current_save_id=lambda: cur["id"]
def digest(d):
    h=hashlib.md5()
    for r,_,fs in sorted(os.walk(d)):
        for f in sorted(fs): h.update(f.encode()); h.update(open(os.path.join(r,f),"rb").read())
    return h.hexdigest()
fails=[]
def chk(label, cond):
    print(("PASS " if cond else "FAIL ")+label)
    if not cond: fails.append(label)
def save(slot):
    sid._last_handled_save_id = cur["id"]
    sid._on_real_save(types.SimpleNamespace(slot_id=slot, slot_name="n"))
    cur["id"] = sid._last_real_save_id
src_digest=digest(os.path.join(root,A))
# 1. normal save to same slot: nothing copied
NOTES.clear(); save(0x1115)
chk("normal save: no copy, no notice", not NOTES and sorted(os.listdir(root))==[A])
# 2. autosave (sentinel): ignored
before=sorted(os.listdir(root)); save(0xffffffff); cur["id"]=A
chk("autosave: nothing happens", sorted(os.listdir(root))==before)
# 3. Save As to a new slot
NOTES.clear(); save(0x2222)
B="Slot_00002222"
chk("save as new slot: copied identically", os.path.isdir(os.path.join(root,B)) and digest(os.path.join(root,B))==src_digest)
chk("save as: source untouched", digest(os.path.join(root,A))==src_digest)
chk("save as: notice shown", any("came along" in n for n in NOTES))
chk("save as: now using the new folder", cur["id"]==B)
# 4. Save As over a slot that already has other data
C="Slot_00003333"; os.makedirs(os.path.join(root,C)); open(os.path.join(root,C,"SimBios.json"),"w").write('{"other":"save"}')
cur["id"]=A; save(0x3333)
backs=[d for d in os.listdir(root) if d.startswith(C+".before-save-as-")]
chk("over existing: old data backed up, not deleted", len(backs)==1 and open(os.path.join(root,backs[0],"SimBios.json")).read()=='{"other":"save"}')
chk("over existing: now has this game's data", digest(os.path.join(root,C))==src_digest)
# 5. second Save As over the same slot same day -> second backup name
cur["id"]=A; save(0x3333)
backs=[d for d in os.listdir(root) if d.startswith(C+".before-save-as-")]
chk("repeat: second backup kept separately", len(backs)==2)
# 6. dormant (no current folder): no copy
cur["id"]=None; n0=len(os.listdir(root)); save(0x4444)
chk("dormant: no copy", not os.path.exists(os.path.join(root,"Slot_00004444")) or not os.listdir(os.path.join(root,"Slot_00004444")))
print("RESULT:", "ALL PASS" if not fails else fails)
shutil.rmtree(T)
