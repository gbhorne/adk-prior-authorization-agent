path = 'agents/prior_auth/tools/pas_submit.py'
with open(path, 'r') as f:
    src = f.read()

old = '    base = crd_base.replace("/cds-services", "").rstrip("/")'
new = '    base = crd_base.replace("/cds-services", "").replace("/crd", "").rstrip("/")'

src = src.replace(old, new)
with open(path, 'w') as f:
    f.write(src)
print('patched')