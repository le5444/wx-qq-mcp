"""Conservative source-release checks. Never print a matched secret value."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {'runtime','exports','cache','models','downloads','backups','.venv','venv','__pycache__'}
FORBIDDEN_SUFFIXES = {'.dpapi','.db','.sqlite','.sqlite3','.wal','.shm','.exe','.dll','.onnx','.bin','.pt','.safetensors',
                      '.wav','.silk','.pcm','.mp3','.mp4','.dat','.png','.jpg','.jpeg','.gif','.webp','.zip','.tar','.bz2'}
PATTERNS = {
    'access-token-like literal': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{24,})\b'),
    'personal Windows home path': re.compile(r'(?i)[A-Z]:[\\/]+Users[\\/]+(?!Public\b)[^\s\"\'<>]+'),
    'real-looking WeChat identifier': re.compile(r'\bwxid_[a-z0-9]{14,}\b'),
    'real-looking QQ user identifier': re.compile(r'\bu_[A-Za-z0-9_-]{20,}\b'),
}


def candidate_files():
    result = subprocess.run(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT,capture_output=True)
    if result.returncode == 0:
        return sorted({ROOT/name for name in result.stdout.decode('utf-8').split('\x00') if name})
    excluded = FORBIDDEN_PARTS | {'.git','build','dist','.pytest_cache'}
    return sorted(p for p in ROOT.rglob('*') if p.is_file() and not any(part in excluded or part.endswith('.egg-info') for part in p.relative_to(ROOT).parts))


def main():
    problems=[];files=candidate_files()
    for path in files:
        relative=path.relative_to(ROOT)
        if any(part in FORBIDDEN_PARTS for part in relative.parts) or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            problems.append((str(relative),'private/runtime/binary file in source release'));continue
        if path.stat().st_size>2_000_000:
            problems.append((str(relative),'unexpectedly large source file'));continue
        try:text=path.read_text(encoding='utf-8-sig')
        except (UnicodeError,OSError):
            problems.append((str(relative),'non-text or unreadable source file'));continue
        for label,pattern in PATTERNS.items():
            match=pattern.search(text)
            if match:
                line=text[:match.start()].count('\n')+1
                problems.append((f'{relative}:{line}',label))
    for path,label in problems:
        print(f'FAIL {path}: {label}')
    print(f'Checked {len(files)} source files; findings: {len(problems)}')
    return 1 if problems else 0


if __name__=='__main__':
    sys.exit(main())
