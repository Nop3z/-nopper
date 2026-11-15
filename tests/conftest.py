# -*- coding: utf-8 -*-
"""pytest 根目录接入：把仓库根加进 sys.path（hwctl 包在仓库根）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
