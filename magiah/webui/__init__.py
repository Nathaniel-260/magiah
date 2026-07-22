# -*- coding: utf-8 -*-
"""magiah.webui — the new Hebrew review interface (RTL SPA + JSON API).

Modules:
    hebrew  — single source of truth for all Hebrew labels / explanations.
    db      — ui_review.db importer, query layer, status writes, decisions.db
              sync-back and migration.
    xlsx    — pure-stdlib XLSX writer (RTL sheets).
    export  — Excel-per-origin export + legacy to_send/ export.
    server  — ThreadingHTTPServer serving the JSON API and static SPA files.
"""
