path = 'agents/prior_auth/tools/coverage_check.py'
with open(path, 'r') as f:
    src = f.read()

old = '    identifiers = payor.get("identifier", [])\n    if identifiers:\n        return identifiers[0].get("value", "UNKNOWN_PAYER")'
new = '    identifiers = payor.get("identifier", [])\n    if isinstance(identifiers, dict):\n        return identifiers.get("value", "UNKNOWN_PAYER")\n    if isinstance(identifiers, list) and identifiers:\n        return identifiers[0].get("value", "UNKNOWN_PAYER")'

src = src.replace(old, new)
with open(path, 'w') as f:
    f.write(src)
print('patched')