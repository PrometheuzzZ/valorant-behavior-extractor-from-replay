# Lets `python -m unittest tools.tests.<module>` import the modules beside it, as discover does.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
