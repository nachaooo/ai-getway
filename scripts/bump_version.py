import re

with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

m = re.search(r'VERSION = "([^"]+)"', content)
if not m:
    print('0.0.0')
    exit()

parts = m.group(1).split('.')
parts[2] = str(int(parts[2]) + 1)
new_version = '.'.join(parts)
content = content.replace(m.group(1), new_version)

with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)

print(new_version)
