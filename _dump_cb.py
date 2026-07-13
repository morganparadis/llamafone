import marshal, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
pyc = sys.argv[1]
with open(pyc, 'rb') as f:
    f.read(16)
    code = marshal.load(f)
print(f"=== {pyc} ===")
print("co_names:", code.co_names)
for c in code.co_consts:
    if hasattr(c, 'co_name'):
        print(f"  <code {c.co_name}>")
        print(f"    co_names: {c.co_names}")
        print(f"    co_consts: {[x for x in c.co_consts if not hasattr(x, 'co_name')]}")
