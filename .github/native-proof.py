import hashlib
import json
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1]).resolve()
destination = pathlib.Path(sys.argv[2]).resolve()
def git(*args):
    return subprocess.check_output(['git', '-C', str(root), *args])
entries = []
for record in git('ls-files', '-s', '-z').decode().split('\0'):
    if record:
        index, name = record.split('\t', 1)
        mode, blob, stage = index.split()
        data = (root / name).read_bytes()
        entries.append({'path': name, 'mode': mode, 'blob': blob,
                        'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
modified = git('diff', '--name-only', 'HEAD').decode().splitlines()
result = {'head': git('rev-parse', 'HEAD').decode().strip(),
          'modified': modified, 'tracked': entries}
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(result, indent=2) + '\n')
if modified:
    raise SystemExit('Tracked source changed: ' + repr(modified))
