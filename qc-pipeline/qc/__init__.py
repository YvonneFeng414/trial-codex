"""SAP faithfulness QC: checks each generated SAP's claims and citations against its PDF."""

from __future__ import annotations

import sys
from pathlib import Path

QC_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = QC_ROOT.parent
TOOLS_DIR = REPO_ROOT / "tools"

# claims.py reuses the generator's own parsers from tools/lint_sap.py rather than forking them.
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
