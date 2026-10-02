from pathlib import Path
import subprocess
if __name__ == '__main__':
    subprocess.run([str(Path(__file__).resolve().parent / '.venv' / 'bin' / 'python'), str(Path(__file__).resolve().parent / 'validate_reconciliation.py')], check=True)
