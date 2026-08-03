"""Recalculate an xlsx in place via LibreOffice UNO (Windows-friendly).

NOT run with the project venv: metrics_report.py invokes this with
LibreOffice's *bundled* python.exe (the only interpreter that has `uno`),
against a headless soffice it starts on localhost:2002 with a throwaway
profile. Needed because LibreOffice does not recalculate xlsx formulas on
open, and openpyxl writes formulas with no cached values — without this
step the report shows blanks in Calc.

Usage: <LibreOffice>/program/python.exe recalc_uno.py <path-to-xlsx>
"""
import sys
import time

import uno
from com.sun.star.beans import PropertyValue


def prop(name, value):
    p = PropertyValue()
    p.Name = name
    p.Value = value
    return p


def main(path):
    local_ctx = uno.getComponentContext()
    resolver = local_ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_ctx)
    ctx = None
    for _ in range(30):  # soffice may still be starting up
        try:
            ctx = resolver.resolve(
                "uno:socket,host=localhost,port=2002;urp;"
                "StarOffice.ComponentContext")
            break
        except Exception:
            time.sleep(1)
    if ctx is None:
        print("ERROR: could not connect to headless soffice on port 2002")
        return 1

    desktop = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.frame.Desktop", ctx)
    doc = desktop.loadComponentFromURL(
        uno.systemPathToFileUrl(path), "_blank", 0, (prop("Hidden", True),))
    if doc is None:
        print("ERROR: could not load document")
        return 1
    try:
        doc.calculateAll()
        doc.store()
    finally:
        doc.close(False)
    print("RECALC OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
