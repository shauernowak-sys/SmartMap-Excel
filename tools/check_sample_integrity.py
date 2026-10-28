"""只读检查 T01 样本基线；不更新基线、不修改输入文件。"""
import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = root / 'reports' / 'environment' / 'baseline.json'
    if not manifest.is_file():
        print('缺少本地基线：reports/environment/baseline.json；请恢复原始基线，不要重新生成以掩盖变更。')
        return 2
    baseline = json.loads(manifest.read_text(encoding='utf-8'))
    failures = 0
    for item in baseline['files']:
        if not item['path'].startswith('samples/'):
            continue
        path = root / item['path']
        if not path.is_file():
            print(f"MISSING: {item['path']}")
            failures += 1
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        ok = digest == item['sha256'] and path.stat().st_size == item['bytes']
        print(f"{'OK' if ok else 'CHANGED'}: {item['path']}")
        failures += not ok
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
