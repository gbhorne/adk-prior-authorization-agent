path = 'agents/prior_auth/tools/coverage_check.py'
with open(path, 'r') as f:
    src = f.read()

old = '                f"{crd_url}/order-sign",'
new = '                f"{crd_url}" if crd_url.endswith("/crd") else f"{crd_url}/order-sign",'

src = src.replace(old, new)
with open(path, 'w') as f:
    f.write(src)
print('patched')