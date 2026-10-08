# -*- coding: utf-8 -*-
"""Double-click launcher for the magiah Hebrew review UI.

Frozen into a single Windows EXE with PyInstaller (see magiah.spec) so a
non-technical user needs no Python install: double-click the exe and the
review server starts and the browser opens.

Data/out dir resolution (first match wins):
  1. --out <dir> on the command line (e.g. from a shortcut).
  2. the MAGIAH_OUT environment variable.
  3. a "magiah_data" folder next to the exe (created if missing).

The console window is kept open on any startup error so a double-click user
can read the Hebrew message instead of the window vanishing.
"""
import multiprocessing
import os
import socket
import sys


def _app_dir():
    """Directory the exe lives in (or this script's dir when run as .py)."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _resolve_outdir(argv):
    """Where the data folder lives. Created if missing, whichever way it was
    named: sqlite cannot open a database inside a directory that is not there,
    so a not-yet-created --out folder would leave every request failing with a
    500 and a UI that loads but shows nothing."""
    # 1. --out argument (supports "--out X" and "--out=X")
    for i, a in enumerate(argv):
        if a == '--out' and i + 1 < len(argv):
            return _ensure(argv[i + 1])
        if a.startswith('--out='):
            return _ensure(a[len('--out='):])
    # 2. environment variable
    env = os.environ.get('MAGIAH_OUT')
    if env:
        return _ensure(env)
    # 3. magiah_data next to the exe
    return _ensure(os.path.join(_app_dir(), 'magiah_data'))


def _ensure(path):
    d = os.path.abspath(path)
    os.makedirs(d, exist_ok=True)
    return d


def _free_port(start=8766, tries=50):
    """First bindable port on 127.0.0.1 from `start` upward."""
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    return start


def _pause(msg):
    """Print a message and hold the console open for a double-click user."""
    print(msg, flush=True)
    try:
        input('\nהקש Enter לסגירה... (Press Enter to close) ')
    except (EOFError, KeyboardInterrupt):
        pass


STAGES = ('lexicon', 'calibrate', 'detect', 'locate', 'report', 'all', 'book')


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    # Stage mode: the scan runner re-invokes this exe as `<exe> <stage> --out
    # <dir>` to run one pipeline stage as a subprocess. A frozen exe is not a
    # Python interpreter, so it cannot be driven with `-m magiah <stage>` —
    # without this branch it would just open a second UI and never scan.
    argv = [a for a in sys.argv[1:] if a not in ('-X', 'utf8')]
    if argv and argv[0] in STAGES:
        from magiah.cli import main as cli_main
        sys.argv = ['magiah'] + argv
        return cli_main() or 0

    try:
        outdir = _resolve_outdir(sys.argv[1:])
    except OSError as e:
        _pause('לא ניתן ליצור את תיקיית הנתונים. בדוק את הנתיב שנתת ב־--out '
               'ואת ההרשאות לתיקייה. פרטים: %s' % e)
        return 1
    port = _free_port()

    try:
        from magiah.webui import server as webui_server
    except Exception:
        import traceback
        traceback.print_exc()
        _pause('שגיאה בטעינת מגיה. ודא שקובץ ההפעלה שלם.')
        return 1

    print('מַגִּיהַּ — ממשק סקירה', flush=True)
    print('תיקיית הנתונים: ' + outdir, flush=True)
    print('כתובת: http://127.0.0.1:%d/' % port, flush=True)
    print('לסגירה: סגור חלון זה (Ctrl+C).', flush=True)

    try:
        webui_server.serve(outdir, port=port, open_browser=True)
    except KeyboardInterrupt:
        return 0
    except OSError as e:
        import traceback
        traceback.print_exc()
        _pause('לא ניתן להפעיל את השרת (ייתכן שהפורט תפוס). פרטים: %s' % e)
        return 1
    except Exception:
        import traceback
        traceback.print_exc()
        _pause('אירעה שגיאה בהפעלת השרת. פרטי השגיאה מופיעים למעלה.')
        return 1
    return 0


if __name__ == '__main__':
    # Required before anything else in a frozen build: the pipeline uses
    # multiprocessing.Pool, and without this each worker re-runs the exe from
    # the top instead of executing its task — the scan then hangs forever.
    multiprocessing.freeze_support()
    sys.exit(main())
