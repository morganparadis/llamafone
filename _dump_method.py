import marshal, sys, io, dis
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
pyc = sys.argv[1]
target = sys.argv[2]
with open(pyc, 'rb') as f:
    f.read(16)
    code = marshal.load(f)

def walk(co):
    if hasattr(co, 'co_name') and co.co_name == target:
        print(f"=== {co.co_name} ===")
        print(f"argcount: {co.co_argcount}, kwonly: {co.co_kwonlyargcount}")
        print(f"co_varnames[:argcount]: {co.co_varnames[:co.co_argcount]}")
        print(f"co_names:  {co.co_names}")
        print("--- dis ---")
        for ins in dis.get_instructions(co):
            print(f"  {ins.opname:24} {ins.argval!r}")
        print()
    if hasattr(co, 'co_consts'):
        for c in co.co_consts:
            if hasattr(c, 'co_name'):
                walk(c)

walk(code)
