# Project Rules & UX Conventions

## Data Entry & Caret Focus
- On every page with a default data entry field (e.g. Sales screen, Shift Tracker, Inventory Receive, Returns), caret focus MUST automatically return/stay inside the default data entry input field after every action or scan.

## Audio Feedback
- The user MUST ALWAYS receive instant audio feedback (distinct high beep for success, distinct low/square tone for error) whenever a scan occurs (hardware scanner or camera).

## Alert Banner Persistence
- Success and error alert banners MUST remain persistent until the next scan or user action occurs. Do NOT auto-dismiss or hide alert status indicators after a timer.

## PDF & Report Stability (Production Safety)
- PDF generation strings MUST ALWAYS be sanitized/wrapped in `pdf_safe()` to prevent `FPDFUnicodeEncodingException` when non-ASCII characters (e.g. em-dashes `\u2014`, smart quotes `’`, etc.) are rendered. Report endpoints (`/reconciliation`, `/reconciliation/pdf`, `/export_shift`) MUST ALWAYS be verified to return HTTP 200 clean success before deploying any changes.
